from __future__ import annotations
import asyncio
from module.memory_management.shared.contracts.errors import MemoryModelUnavailable
from module.memory_management.topic.core.domain.retrieval import (
    ObjectRerankPair,
    ObjectRerankScore,
    TopicRerankPair,
    TopicRerankScore,
)
from module.memory_management.topic.core.processing.config import MemoryRerankConfig
from module.memory_management.topic.core.processing.prompts.object_rerank import (
    OBJECT_CONTENT_RERANK_INSTRUCTION,
    OBJECT_NAME_RERANK_INSTRUCTION,
)
from module.memory_management.topic.core.processing.prompts.topic_rerank import TOPIC_RERANK_INSTRUCTION
from threading import Lock


class QwenMemoryRerankRepository:
    """本地Qwen事务重排；与SQLite解耦，实例由应用持有，不在请求中反复加载。"""
    def __init__(self, model, tokenizer, config: MemoryRerankConfig) -> None:
        self._model, self._tokenizer, self._config = model, tokenizer, config
        self._lock = Lock()

    @classmethod
    async def load(cls, config: MemoryRerankConfig):
        return await asyncio.to_thread(cls._load, config)

    @classmethod
    def _load(cls, config: MemoryRerankConfig):
        import torch
        from transformers import AutoTokenizer, AutoModelForCausalLM
        if config.device.startswith("cuda") and not torch.cuda.is_available():
            raise ValueError("reranker配置为CUDA，但当前PyTorch不能使用CUDA")
        tokenizer = AutoTokenizer.from_pretrained(config.model_path, padding_side="left", local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(config.model_path, local_files_only=True,
            dtype=torch.float16 if config.device.startswith("cuda") else torch.float32,
            attn_implementation="sdpa").to(config.device).eval()
        return cls(model, tokenizer, config)

    @staticmethod
    def _input_text(pair: ObjectRerankPair) -> str:
        # 沿用 Qwen3-Reranker 官方 yes/no 评分模板，不生成解释或思维链。
        instruction = OBJECT_NAME_RERANK_INSTRUCTION if pair.section_key == "name" else OBJECT_CONTENT_RERANK_INSTRUCTION
        return QwenMemoryRerankRepository._format_input(pair.query, pair.document, instruction)

    @staticmethod
    def _format_input(query: str, document: str, instruction: str) -> str:
        return ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
                'and the Instruct provided. Note that the answer can only be "yes" or "no".'
                '<|im_end|>\n<|im_start|>user\n'
                f'<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {document}'
                '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n')

    def _score_texts(self, texts: tuple[str, ...]) -> tuple[float, ...]:
        import torch
        # object/topic借用同一模型和锁；取消等待后，底层推理仍受锁保护。
        with self._lock, torch.inference_mode():
            if self._model is None:
                raise RuntimeError("reranker已关闭")
            no_id = self._tokenizer.convert_tokens_to_ids("no")
            yes_id = self._tokenizer.convert_tokens_to_ids("yes")
            results = []
            for start in range(0, len(texts), self._config.batch_size):
                inputs = self._tokenizer(list(texts[start:start + self._config.batch_size]),
                    padding=True, truncation=False, return_tensors="pt")
                if inputs["input_ids"].shape[1] > self._config.max_length:
                    raise ValueError("reranker输入超过配置容量，拒绝静默截断正文")
                inputs = inputs.to(self._model.device)
                logits = self._model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :].float()
                results.extend(logits[:, [no_id, yes_id]].softmax(dim=-1)[:, 1].cpu().tolist())
            return tuple(results)

    async def batch_object_rerank(self, pairs: tuple[ObjectRerankPair, ...]) -> tuple[ObjectRerankScore, ...]:
        if not pairs:
            return ()
        try:
            scores = await asyncio.to_thread(self._score_texts, tuple(self._input_text(pair) for pair in pairs))
            return tuple(ObjectRerankScore(pair.source_object_id, pair.object_id, score, pair.section_key)
                         for pair, score in zip(pairs, scores, strict=True))
        except Exception as error:
            raise MemoryModelUnavailable(f"object重排未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    async def batch_topic_rerank(self, pairs: tuple[TopicRerankPair, ...]) -> tuple[TopicRerankScore, ...]:
        if not pairs:
            return ()
        try:
            texts = tuple(self._format_input(pair.query, pair.document, TOPIC_RERANK_INSTRUCTION) for pair in pairs)
            scores = await asyncio.to_thread(self._score_texts, texts)
            return tuple(TopicRerankScore(pair.source_object_id, pair.topic_id, score)
                         for pair, score in zip(pairs, scores, strict=True))
        except Exception as error:
            raise MemoryModelUnavailable(f"topic重排未完成（{type(error).__name__}）",
                retryable=not isinstance(error, (ValueError, TypeError))) from error

    def close(self) -> None:
        with self._lock:
            self._model = self._tokenizer = None
