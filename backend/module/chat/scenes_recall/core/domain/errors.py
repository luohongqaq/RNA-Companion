"""场景召回能力尚未就绪时的错误。"""


class SceneRecallUnavailable(RuntimeError):
    """场景索引未启动，当前不能提供检索。"""
