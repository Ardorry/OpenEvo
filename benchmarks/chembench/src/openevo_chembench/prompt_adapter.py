"""Strict one-way projection from private ChemBench tasks to public prompts."""

from __future__ import annotations

from openevo_chembench.models import PrivateTask, PublicChoice, PublicPrompt


MULTIPLE_CHOICE_ANSWER_FORMAT = (
    "Return exactly one choice label inside <answer>...</answer>."
)
FREE_RESPONSE_ANSWER_FORMAT = "Return the final answer inside <answer>...</answer>."


class ChemBenchPromptAdapter:
    """Project exact ``PrivateTask`` values into the closed public DTO."""

    __slots__ = ()

    def project(self, task: PrivateTask) -> PublicPrompt:
        """Copy only question text and public option text into ``PublicPrompt``."""

        if type(task) is not PrivateTask:
            raise TypeError("ChemBenchPromptAdapter.project requires an exact PrivateTask")
        choices = tuple(
            PublicChoice(label=_choice_label(index), text=option)
            for index, option in enumerate(task.options)
        )
        answer_format = (
            MULTIPLE_CHOICE_ANSWER_FORMAT
            if choices
            else FREE_RESPONSE_ANSWER_FORMAT
        )
        return PublicPrompt(
            question=task.question,
            choices=choices,
            answer_format=answer_format,
        )


def project_private_task(task: PrivateTask) -> PublicPrompt:
    """Convenience entrypoint with the same strict type boundary."""

    return ChemBenchPromptAdapter().project(task)


def _choice_label(index: int) -> str:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise ValueError("choice index must be a non-negative integer")
    label = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


__all__ = [
    "FREE_RESPONSE_ANSWER_FORMAT",
    "MULTIPLE_CHOICE_ANSWER_FORMAT",
    "ChemBenchPromptAdapter",
    "project_private_task",
]
