"""消息正文携带的视觉状态及其姿势、表情标识。"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Pose:
    """一个姿势的内部 ID 和名称。"""
    pose_id: str
    pose_name: str


@dataclass(frozen=True, slots=True)
class Expression:
    """一个表情的内部 ID 和名称。"""
    expression_id: str
    expression_name: str


@dataclass(frozen=True, slots=True)
class VisualState:
    """回复对应的视觉状态，包含姿势和表情。"""
    pose_name: Pose
    expression_name: Expression

    @classmethod
    def from_names(cls, pose_name: str, expression_name: str) -> "VisualState":
        """Use names as keys when no asset ID resolver is configured."""
        return cls(Pose(pose_name, pose_name), Expression(expression_name, expression_name))

    def names(self) -> dict[str, str]:
        return {
            "pose_name": self.pose_name.pose_name,
            "expression_name": self.expression_name.expression_name,
        }
