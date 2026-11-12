r"""本地全量回放：把旧桌宠的对话按原始时间重放进当前系统。

只在本机使用，已被 .gitignore 排除，不属于产品代码。

用法：
    backend\.venv\Scripts\python.exe scripts\replay_eval.py --xlsx D:\path\messages.xlsx

前提：后端用同一个 --data-root 启动，并设置
    RNA_MEMORY_ENABLED=true
    RNA_MEMORY_PRODUCTION_ENABLED=true
    RNA_CONTEXT_ENABLED=true
    RNA_CONTEXT_SESSION_TARGET_TOKENS=100000   # 关闭自动换窗口，由本脚本在段末触发
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import html
import json
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import openpyxl

API = "/api/v1"
REPLY_TIMEOUT_SECONDS = 180.0
REPLY_POLL_SECONDS = 0.5
DATABASE_NAME = "rna_companion.db"
UTC = timezone.utc


@dataclass(slots=True)
class Message:
    """旧数据里的一条用户消息。"""

    role: str
    content: str
    created_at: datetime


@dataclass(slots=True)
class Segment:
    """一段旧会话：用户连续聊到关掉窗口为止。"""

    session_id: int
    messages: list[Message] = field(default_factory=list)

    def user_messages(self) -> list[Message]:
        return [message for message in self.messages if message.role == "user"]


# 记忆正文里指代另一位对话者的第三人称；“其他/其它”不算。
_TA = re.compile("(?<!其)他")


@dataclass(slots=True)
class TurnResult:
    """一轮回放的产物，段末统一改时间并写稿。"""

    turn_id: str
    moment: datetime
    query: str
    reply: str
    audit: dict | None
    seconds: float
    memory_text: str = ""


@dataclass(slots=True)
class ReplayOptions:
    """命令行参数。"""

    xlsx: Path
    data_root: Path
    character: str
    api: str
    output: Path
    limit: int
    skip: int
    resume: bool

    @classmethod
    def parse(cls) -> "ReplayOptions":
        parser = argparse.ArgumentParser(description="把旧对话按原始时间重放进当前系统")
        parser.add_argument("--xlsx", type=Path, required=True, help="旧桌宠导出的 messages.xlsx")
        parser.add_argument("--data-root", type=Path, required=True, help="后端使用的 RNA_DATA_ROOT")
        parser.add_argument("--character", default="character_deepseek", help="回放使用的角色ID")
        parser.add_argument("--api", default="http://127.0.0.1:8765", help="后端地址")
        parser.add_argument("--output", type=Path,
                            default=Path(__file__).resolve().parents[1] / "var" / "replay-out",
                            help="产物目录，默认放在 backend/var/replay-out")
        parser.add_argument("--limit", type=int, default=0, help="只回放 N 段，0 表示到结尾")
        parser.add_argument("--skip", type=int, default=0, help="跳过前 N 段，用于分段接力")
        parser.add_argument("--resume", action="store_true", help="接着上次的产物继续写")
        arguments = parser.parse_args()
        return cls(arguments.xlsx, arguments.data_root, arguments.character,
                   arguments.api, arguments.output, arguments.limit, arguments.skip,
                   arguments.resume)


class SpreadsheetReader:
    """把 xlsx 读成按会话分组的消息序列。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def segments(self) -> list[Segment]:
        workbook = openpyxl.load_workbook(self._path, read_only=True, data_only=True)
        try:
            rows = [row for row in list(workbook["messages"].iter_rows(values_only=True))[1:]
                    if any(cell is not None for cell in row)]
        finally:
            workbook.close()
        grouped: dict[int, list[Message]] = {}
        for row in rows:
            grouped.setdefault(int(row[1]), []).append(Message(
                role=str(row[2]),
                content=(row[3] or "").strip(),
                created_at=datetime.strptime(str(row[4]), "%Y-%m-%d %H:%M:%S"),
            ))
        return [Segment(session_id, sorted(items, key=lambda item: item.created_at))
                for session_id, items in sorted(grouped.items())]


class TimeShift:
    """整体平移时间线：保留间隔与时分秒，让最后一段落在昨天。"""

    def __init__(self, segments: list[Segment], *, today: date) -> None:
        self._zone = datetime.now().astimezone().tzinfo
        latest = max(message.created_at for segment in segments for message in segment.messages)
        anchor = datetime.combine(today - timedelta(days=1), latest.timetz()).replace(tzinfo=self._zone)
        self._offset = anchor - latest.replace(tzinfo=self._zone)

    def at(self, moment: datetime) -> datetime:
        """返回带本机时区的历史时刻。"""
        return moment.replace(tzinfo=self._zone) + self._offset


class ReplayDatabase:
    """直接读写后端 SQLite：改写时间、读取召回审计、汇总产出。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def rewrite_turns(self, turns: list[tuple[datetime, str]]) -> None:
        """段末一次性改写整段消息时间；daily按消息时间归属日期。"""
        if not turns:
            return
        with closing(self._connect()) as connection, connection:
            connection.executemany(
                "UPDATE messages SET created_at=? WHERE turn_id=?",
                [(moment.astimezone(UTC).isoformat(), turn_id) for moment, turn_id in turns])

    def rewrite_session(self, session_id: str, started: datetime, ended: datetime) -> None:
        self._write("UPDATE sessions SET session_start_time=?, session_end_time=? WHERE session_id=?",
                    (started.astimezone(UTC).isoformat(), ended.astimezone(UTC).isoformat(), session_id))

    def audit(self, turn_id: str) -> dict | None:
        """读取该轮召回审计；记忆关闭或尚未准备时为空。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT memory_audit_json FROM generation_attempts WHERE turn_id=? "
                "ORDER BY attempt_version DESC LIMIT 1", (turn_id,)).fetchone()
        return None if row is None or row[0] is None else json.loads(row[0])

    def memory_text(self, turn_id: str) -> str:
        """读取这一轮真正拼进提示词的记忆摘要正文；没有记忆时为空串。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT frozen_prompt_json FROM generation_attempts WHERE turn_id=? "
                "ORDER BY attempt_version DESC LIMIT 1", (turn_id,)).fetchone()
        if row is None or row[0] is None:
            return ""
        prompt = json.loads(row[0]) if isinstance(row[0], str) else row[0]
        text = prompt.get("memory_summary") if isinstance(prompt, dict) else None
        return text if isinstance(text, str) else ""

    def memories(self) -> list[dict]:
        """读出已落库的 daily 与 topic 正文，供页面展示。"""
        with closing(self._connect()) as connection:
            daily = [{"kind": "daily", "label": str(day), "doc": json.loads(document)}
                     for day, document in connection.execute(
                         "SELECT memory_date, document_json FROM daily_memories ORDER BY memory_date")]
            topics = [{"kind": "topic", "label": "主题", "doc": json.loads(document)}
                      for (document,) in connection.execute(
                          "SELECT document_json FROM topic_memories "
                          "WHERE document_json IS NOT NULL ORDER BY published_at")]
        return daily + topics

    def pending_jobs(self) -> int:
        """还没跑完的记忆生产任务数；封存后用它等后台把记忆写完。"""
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT count(*) FROM memory_jobs WHERE status NOT IN ('done','obsolete','failed')"
            ).fetchone()[0]

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            return {name: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                    for name, table in (("daily", "daily_memories"), ("object", "memory_objects"),
                                        ("topic", "topic_memories"))}

    def _write(self, statement: str, parameters: tuple) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(statement, parameters)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection


class ChatApi:
    """回放需要的那几个接口。"""

    def __init__(self, client: httpx.AsyncClient, character_id: str) -> None:
        self._client = client
        self._character_id = character_id

    async def create_conversation(self) -> tuple[str, str]:
        response = await self._client.post(f"{API}/conversations", json={
            "character_id": self._character_id, "with_session": True,
        })
        response.raise_for_status()
        body = response.json()
        return body["conversation_id"], body["current_session_id"]

    async def send(self, conversation_id: str, content: str) -> dict:
        response = await self._client.post(f"{API}/conversations/{conversation_id}/messages", json={
            "request_id": f"replay-{uuid.uuid4().hex}", "content": content,
            "character_id": self._character_id,
        })
        response.raise_for_status()
        return response.json()

    async def conversation(self, conversation_id: str) -> dict:
        response = await self._client.get(f"{API}/conversations/{conversation_id}")
        response.raise_for_status()
        return response.json()

    async def messages(self, conversation_id: str, limit: int = 20) -> list[dict]:
        response = await self._client.get(f"{API}/conversations/{conversation_id}/messages",
                                          params={"limit": limit})
        response.raise_for_status()
        return response.json()["items"]

    async def run_retention(self, target: date) -> int:
        """按虚拟日期推进一次降池；维护路由不在 /api/v1 前缀下。"""
        response = await self._client.post("/maintenance/retention",
                                           json={"target_date": target.isoformat()})
        response.raise_for_status()
        return response.json()["cooled"]

    async def seal(self, conversation_id: str, session_id: str) -> dict:
        response = await self._client.post(
            f"{API}/conversations/{conversation_id}/sessions/{session_id}/seal")
        response.raise_for_status()
        return response.json()


class TranscriptWriter:
    """把回放过程写成可读对话稿与指标表，方便回看人设漂移。"""

    def __init__(self, output: Path, *, resume: bool = False, memories=None) -> None:
        self._output = output
        self._memories = memories
        self._store = output / "replay_turns.json"
        self._turns: list[tuple[int, datetime, str, str, dict | None, str]] = (
            self._load() if resume and self._store.exists() else [])
        mode = "a" if self._turns else "w"
        self._transcript = (output / "replay_transcript.md").open(mode, encoding="utf-8")
        self._metrics = (output / "replay_metrics.csv").open(mode, encoding="utf-8-sig", newline="")
        self._rows = csv.writer(self._metrics)
        if mode == "w":
            self._rows.writerow(["seq", "segment", "at", "query", "reply", "recall_status",
                                 "reason_code", "recall_seconds", "selected",
                                 "mem_chars", "mem_user", "mem_ta", "mem_chief"])
            self._transcript.write("# 回放对话稿\n\n")
        self._sequence = len(self._turns)
        self._finished = False
        self._write_html()

    def turn(self, segment: int, moment: datetime, query: str, reply: str,
             audit: dict | None, seconds: float, memory_text: str = "") -> None:
        self._sequence += 1
        audit = audit or {}
        selected = len(audit.get("source_refs") or ())
        self._rows.writerow([self._sequence, segment, moment.isoformat(timespec="minutes"),
                             query, reply, audit.get("status", ""), audit.get("reason_code", ""),
                             round(float(audit.get("total_seconds") or 0), 2), selected,
                             len(memory_text), *self._person_counts(memory_text)])
        self._transcript.write(
            f"### {self._sequence} · 段 {segment} · {moment:%m-%d %H:%M}\n\n"
            f"**我**：{query}\n\n**她**：{reply}\n\n")
        self._turns.append((segment, moment, query, reply, audit, memory_text))
        self._write_html()
        # 长跑中途被打断时，已完成的轮次不能还留在缓冲区里。
        self._transcript.flush()
        self._metrics.flush()

    @staticmethod
    def _person_counts(text: str) -> tuple[int, int, int]:
        """记忆正文里三种人称的出现次数；用来判断卡片是否被原样搬运。"""
        if not text:
            return (0, 0, 0)
        return (text.count("用户"), len(_TA.findall(text)), text.count("主人"))

    def close(self) -> None:
        self._finished = True
        self._write_html()
        self._transcript.close()
        self._metrics.close()

    def _write_html(self) -> None:
        """每轮落盘一次，浏览器刷新就能看到最新进度。"""
        (self._output / "replay.html").write_text(self._render_html(), encoding="utf-8")
        self._store.write_text(json.dumps(
            [[segment, moment.isoformat(), query, reply, audit, memory_text]
             for segment, moment, query, reply, audit, memory_text in self._turns],
            ensure_ascii=False), encoding="utf-8")

    def _load(self) -> list[tuple[int, datetime, str, str, dict | None, str]]:
        """读回上一次的轮次，分段接力时页面不会断档；旧产物没有记忆正文。"""
        raw = json.loads(self._store.read_text(encoding="utf-8"))
        return [(item[0], datetime.fromisoformat(item[1]), item[2], item[3], item[4],
                 item[5] if len(item) > 5 else "")
                for item in raw]

    def _render_html(self) -> str:
        """生成可直接双击打开的对话页；召回信息标在每条回复下面。"""
        groups: dict[int, list[tuple[int, datetime, str, str, dict | None, str]]] = {}
        for item in self._turns:
            groups.setdefault(item[0], []).append(item)
        sections, links = [], []
        for segment, items in groups.items():
            links.append(f'<a href="#s{segment}">段 {segment}</a>')
            body, day = [], None
            for _, moment, query, reply, audit, memory_text in items:
                if moment.date() != day:
                    day = moment.date()
                    body.append(f'<div class="day">{day:%Y-%m-%d}</div>')
                body.append(self._bubble(moment, query, reply, audit, memory_text))
            sections.append(f'<section id="s{segment}"><h2>段 {segment}'
                            f'<small>{items[0][1]:%m-%d %H:%M} → {items[-1][1]:%m-%d %H:%M} · '
                            f'{len(items)} 轮</small></h2>{"".join(body)}</section>')
        refresh = "" if self._finished else '<meta http-equiv="refresh" content="8">'
        panel = self._render_memories()
        progress = (f'已回放 {len(self._turns)} 轮' if self._finished
                    else f'已回放 {len(self._turns)} 轮 · 生成中，每 8 秒自动刷新')
        return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{refresh}'
                f'<title>回放对话</title><style>{self._style()}</style></head><body>'
                f'<header><h1>回放对话</h1><p class="progress">{progress}</p>'
                f'<nav>{"".join(links)}</nav></header>'
                f'<main>{panel}{"".join(sections)}</main>{self._script()}</body></html>')

    SECTIONS = (("memorable_moments", "情节"), ("profile_facts", "事实"),
                ("intentions", "打算"), ("commitments", "承诺"), ("reflections", "心声"))

    def _render_memories(self) -> str:
        """按记忆文档的分区摊开，别把不同字段揉成一条流水账。"""
        if self._memories is None:
            return ""
        items = self._memories()
        if not items:
            return '<details class="mem"><summary>记忆库：还没有产出</summary></details>'
        daily = sum(1 for item in items if item["kind"] == "daily")
        blocks = []
        for item in items:
            document = item["doc"]
            title = document.get("title") or document.get("topic") or item["label"]
            blocks.append(f'<div class="mem-item"><h3><span class="tag">{html.escape(item["kind"])}'
                          f'</span>{html.escape(str(item["label"]))} · {html.escape(str(title))}</h3>'
                          f'{self._sections(document)}</div>')
        return (f'<details class="mem"><summary>记忆库：daily {daily} 条 · '
                f'topic {len(items) - daily} 条</summary>{"".join(blocks)}</details>')

    @classmethod
    def _sections(cls, document: dict) -> str:
        """daily 按业务分区展开；其它文档退化成纯文本列表。"""
        parts = []
        for key, label in cls.SECTIONS:
            values = document.get(key) or []
            if not values:
                continue
            lines = "".join(
                f'<li>{html.escape(str(value.get("text") if isinstance(value, dict) else value))}</li>'
                for value in values)
            parts.append(f'<div class="sec"><span class="lab">{label}</span><ul>{lines}</ul></div>')
        if parts:
            return "".join(parts)
        texts = cls._texts(document)[:6]
        return f'<ul>{"".join(f"<li>{html.escape(text)}</li>" for text in texts)}</ul>'

    @staticmethod
    def _texts(node, out=None) -> list[str]:
        """递归收集文档里的 text 字段，兼容 daily 与 topic 的不同结构。"""
        out = [] if out is None else out
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "text" and isinstance(value, str):
                    out.append(value)
                else:
                    TranscriptWriter._texts(value, out)
        elif isinstance(node, list):
            for item in node:
                TranscriptWriter._texts(item, out)
        return out

    @staticmethod
    def _script() -> str:
        """刷新后回到原来的位置，方便一直盯最新的一条。"""
        return ('<script>document.addEventListener("DOMContentLoaded",()=>{'
                'const y=localStorage.getItem("replay-scroll");'
                'if(y)window.scrollTo(0,Number(y));});'
                'window.addEventListener("scroll",()=>'
                'localStorage.setItem("replay-scroll",String(window.scrollY)));</script>')

    @staticmethod
    def _bubble(moment: datetime, query: str, reply: str, audit: dict | None,
                memory_text: str = "") -> str:
        recall = ""
        if audit:
            selected = len(audit.get("source_refs") or ())
            recall = (f'<span class="recall">{html.escape(str(audit.get("status", "")))}'
                      f' · {html.escape(str(audit.get("reason_code", "")))}'
                      f' · {float(audit.get("total_seconds") or 0):.1f}s · {selected} 条</span>')
        panel = ""
        if memory_text.strip():
            users, third, chief = TranscriptWriter._person_counts(memory_text)
            panel = (f'<details class="mt"><summary>本轮注入的记忆 {len(memory_text)} 字 · '
                     f'用户 {users} · 他 {third} · 主人 {chief}</summary>'
                     f'<pre>{html.escape(memory_text)}</pre></details>')
        return (f'<div class="turn"><div class="mine">{html.escape(query)}</div>'
                f'<div class="hers">{html.escape(reply)}{recall}</div>{panel}'
                f'<time>{moment:%H:%M}</time></div>')

    @staticmethod
    def _style() -> str:
        return (
            "body{margin:0;background:#faf7f2;color:#33302b;"
            "font:16px/1.75 system-ui,'Segoe UI','Microsoft YaHei',sans-serif}"
            "header{position:sticky;top:0;z-index:2;background:#fffdf9;border-bottom:1px solid #e8e0d4;"
            "padding:12px 24px}"
            "h1{margin:0 0 6px;font-size:17px}"
            ".progress{margin:0 0 8px;font-size:12px;color:#8a7a63}"
            ".mem{background:#fffdf9;border:1px solid #e8e0d4;border-radius:10px;padding:10px 14px;margin-bottom:24px}"
            ".mem summary{cursor:pointer;font-size:13px;color:#7a6a55}"
            ".mem-item{margin:12px 0}"
            ".mem-item h3{margin:0 0 4px;font-size:13px;color:#5f5446}"
            ".mem-item ul{margin:0;padding-left:18px;font-size:13px;color:#6f6353}"
            ".tag{display:inline-block;margin-right:6px;padding:0 6px;border-radius:999px;"
            "background:#efe6d8;color:#8a7a63;font-size:11px}"
            ".sec{display:flex;gap:8px;margin:6px 0}"
            ".lab{flex:0 0 32px;color:#a3937a;font-size:12px;padding-top:2px}"
            ".sec ul{margin:0}"
            "nav{display:flex;flex-wrap:wrap;gap:6px;max-height:64px;overflow:auto}"
            "nav a{font-size:12px;padding:2px 8px;border:1px solid #ddd2c2;border-radius:999px;"
            "color:#7a6a55;text-decoration:none}"
            "main{max-width:820px;margin:0 auto;padding:24px 16px 80px}"
            "section{margin-bottom:36px}"
            "h2{font-size:14px;color:#8a7a63;border-left:3px solid #d9c9ae;padding-left:8px}"
            "h2 small{margin-left:8px;color:#b0a390;font-weight:400}"
            ".day{text-align:center;color:#b0a390;font-size:12px;margin:18px 0}"
            ".turn{margin:12px 0;display:flex;flex-direction:column;gap:6px}"
            ".mine{align-self:flex-end;max-width:74%;background:#e8f1e6;padding:9px 13px;"
            "border-radius:14px 14px 4px 14px;white-space:pre-wrap}"
            ".hers{align-self:flex-start;max-width:74%;background:#fff;padding:9px 13px;"
            "border-radius:14px 14px 14px 4px;box-shadow:0 1px 3px rgba(0,0,0,.06);white-space:pre-wrap}"
            ".recall{display:block;margin-top:6px;font-size:11px;color:#a89b87}"
            ".mt{margin:2px 0 0 8px;font-size:11px;color:#a89b87}"
            ".mt summary{cursor:pointer}"
            ".mt pre{margin:6px 0;padding:8px 10px;background:#fffdf9;border:1px solid #e8e0d4;"
            "border-radius:8px;white-space:pre-wrap;font:12px/1.7 inherit;color:#6f6353}"
            "time{font-size:11px;color:#c0b5a2}"
        )


class ReplayRunner:
    """按段推进：发消息、等回复、改写时间，段末封存触发记忆生产。"""

    def __init__(self, api: ChatApi, database: ReplayDatabase,
                 writer: TranscriptWriter, shift: TimeShift,
                 conversation_file: Path | None = None) -> None:
        self._api = api
        self._database = database
        self._writer = writer
        self._shift = shift
        # 全程只用一个对话：封存会把当前会话换成后继会话，下一段接着用，
        # 这样上一段的交接摘要才会作为继承上文进入下一段的提示词。
        self._conversation_file = conversation_file
        self._conversation_id: str | None = (
            conversation_file.read_text(encoding="utf-8").strip()
            if conversation_file is not None and conversation_file.exists() else None)

    async def run(self, segments: list[Segment]) -> None:
        for index, segment in enumerate(segments, start=1):
            await self._replay_segment(segment, index, len(segments))

    async def _replay_segment(self, segment: Segment, index: int, total: int) -> None:
        users = segment.user_messages()
        # 进入这一段之前，按这一段的虚拟日期推进一次降池。
        cooled = await self._api.run_retention(self._shift.at(users[0].created_at).date()) if users else 0
        conversation_id, session_id = await self._current_session()
        started = asyncio.get_running_loop().time()
        turns: list[TurnResult] = []
        for position, message in enumerate(users, start=1):
            turns.append(await self._replay_turn(conversation_id, segment, message, position, len(users)))
        # 段末一次性改时间：一次事务写完，避免每轮抢写锁。
        self._database.rewrite_turns([(item.moment, item.turn_id) for item in turns])
        if turns:
            self._database.rewrite_session(session_id, turns[0].moment, turns[-1].moment)
        sealed = await self._seal(conversation_id)
        waited = await self._wait_memory()
        ready = sealed and await self._ensure_successor(conversation_id, session_id)
        if not ready:
            # 两种卡法都要换新对话继续：封存被拒（会话仍 open），或封存已受理但
            # 摘要任务失败（会话停在 waiting_handoff，继续发消息会 409）。
            print("  ! 本段没有可接替的会话，下一段改用新对话", flush=True)
            self._forget_conversation()
        print(f"[{index}/{total}] 段 {segment.session_id}: {len(users)} 轮，降冷 {cooled} 条，封存 "
              f"{'完成' if sealed else '跳过'}，等记忆 {waited:.0f}s，"
              f"合计 {asyncio.get_running_loop().time() - started:.0f}s", flush=True)

    async def _send_with_retry(self, conversation_id: str, content: str) -> dict:
        """交接刚提交时偶发 409（会话仍在等待交接），重试几次再放弃。"""
        for attempt in range(5):
            try:
                return await self._api.send(conversation_id, content)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 409 or attempt == 4:
                    raise
                await asyncio.sleep(3)
        raise RuntimeError("unreachable")

    async def _current_session(self) -> tuple[str, str]:
        """首段新建对话，之后沿用封存后接替的会话；没有可接替的会话就新开一个对话。"""
        if self._conversation_id is None:
            conversation_id, session_id = await self._api.create_conversation()
            self._remember(conversation_id)
            return conversation_id, session_id
        detail = await self._api.conversation(self._conversation_id)
        session_id = detail.get("current_session_id")
        if not session_id:
            # 上一段的摘要没生成出来，接替会话不存在；换新对话继续，避免整轮中断。
            print("  ! 没有可接替的会话，改为新建对话", flush=True)
            conversation_id, session_id = await self._api.create_conversation()
            self._remember(conversation_id)
            return conversation_id, session_id
        return self._conversation_id, session_id

    async def _replay_turn(self, conversation_id: str, segment: Segment,
                           message: Message, position: int, total: int) -> TurnResult:
        started = asyncio.get_running_loop().time()
        sent = await self._send_with_retry(conversation_id, message.content)
        reply = await self._wait_reply(conversation_id, sent["turn_id"])
        seconds = asyncio.get_running_loop().time() - started
        moment = self._shift.at(message.created_at)
        result = TurnResult(sent["turn_id"], moment, message.content, reply,
                            self._database.audit(sent["turn_id"]), seconds,
                            self._database.memory_text(sent["turn_id"]))
        # 每轮立刻写稿，HTML 才能边跑边看。
        self._writer.turn(segment.session_id, moment, message.content, reply,
                          result.audit, seconds, result.memory_text)
        print(f"    段 {segment.session_id} {position}/{total} {seconds:.1f}s", flush=True)
        return result

    async def _wait_reply(self, conversation_id: str, turn_id: str) -> str:
        deadline = asyncio.get_running_loop().time() + REPLY_TIMEOUT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(REPLY_POLL_SECONDS)
            for item in await self._api.messages(conversation_id):
                if item["turn_id"] == turn_id and item["role"] == "character":
                    return item["content"]
        raise TimeoutError(f"回复超时：turn {turn_id}")

    def _forget_conversation(self) -> None:
        """放弃当前对话记录，下一段重新开一个。"""
        self._conversation_id = None
        if self._conversation_file is not None and self._conversation_file.exists():
            self._conversation_file.unlink()

    def _remember(self, conversation_id: str) -> None:
        """写下对话 ID：分批接力时下一批还要接着用同一个对话。"""
        self._conversation_id = conversation_id
        if self._conversation_file is not None:
            self._conversation_file.write_text(conversation_id, encoding="utf-8")

    async def _ensure_successor(self, conversation_id: str, sealed_session_id: str,
                                timeout: float = 120.0) -> bool:
        """等交接流水线把后继会话接上；只有接上了，下一段才拿得到继承上文。"""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < timeout:
            detail = await self._api.conversation(conversation_id)
            current = detail.get("current_session_id")
            if current and current != sealed_session_id:
                return True
            await asyncio.sleep(2)
        return False

    async def _wait_memory(self, timeout: float = 180.0) -> float:
        """等后台把这段的 daily/object/topic 写完，再进入下一段。"""
        loop = asyncio.get_running_loop()
        started = loop.time()
        while loop.time() - started < timeout:
            await asyncio.sleep(2)
            if self._database.pending_jobs() == 0:
                break
        return loop.time() - started

    async def _seal(self, conversation_id: str) -> bool:
        detail = await self._api.conversation(conversation_id)
        session_id = detail["current_session_id"]
        if not session_id:
            return False
        try:
            await self._api.seal(conversation_id, session_id)
        except httpx.HTTPStatusError as exc:
            # 摘要超预算时交接任务会判定失败，服务端据此回滚会话并返回 409。
            # 真实运行时用户可以稍后重试；回放里不能因此中断整轮，改为换新对话继续。
            if exc.response.status_code == 409:
                print(f"  ! 封存被拒绝（409），该段摘要未通过校验：{exc.response.text[:80]}", flush=True)
                return False
            raise
        return True


class Replay:
    """装配参数、数据源与输出目录。"""

    def __init__(self, options: ReplayOptions) -> None:
        self._options = options

    async def run(self) -> int:
        all_segments = SpreadsheetReader(self._options.xlsx).segments()
        # 锚点永远按整份数据算，分段接力才不会错位。
        shift = TimeShift(all_segments, today=datetime.now(UTC).date())
        segments = all_segments[self._options.skip:]
        if self._options.limit:
            segments = segments[: self._options.limit]
        if not segments:
            print("没有可回放的会话", file=sys.stderr)
            return 1
        self._options.output.mkdir(parents=True, exist_ok=True)
        database = ReplayDatabase(self._options.data_root / DATABASE_NAME)
        before = database.counts()
        writer = TranscriptWriter(self._options.output, resume=self._options.resume,
                                  memories=database.memories)
        try:
            async with httpx.AsyncClient(base_url=self._options.api, timeout=300) as client:
                runner = ReplayRunner(ChatApi(client, self._options.character),
                                      database, writer, shift,
                                      self._options.output / "replay-conversation.txt")
                await runner.run(segments)
        finally:
            # 收尾时去掉自动刷新，页面显示最终状态。
            writer.close()
        after = database.counts()
        print("记忆产出：" + "，".join(f"{key} {before[key]}→{after[key]}" for key in after))
        print(f"产物目录：{self._options.output.resolve()}")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(Replay(ReplayOptions.parse()).run()))
