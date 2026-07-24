"""Official-compatible ChemBench4K five-shot and dev-LOO prompt rendering."""

from __future__ import annotations

from dataclasses import dataclass

from openevo_chembench.chembench4k_models import (
    CHEMBENCH4K_CATEGORIES,
    CHEMBENCH4K_REVISION,
    PrivateChemBench4KTask,
    PublicChemBench4KTask,
    RenderedChemBench4KPrompt,
)


OFFICIAL_INSTRUCTION = (
    "There is a single choice question about chemistry. "
    "Answer the question by replying A, B, C or D."
)


def _question_block(
    task: PublicChemBench4KTask,
    *,
    demonstration_answer: str | None = None,
) -> str:
    block = (
        f"{OFFICIAL_INSTRUCTION}\n"
        f"Question: {task.question}\n"
        f"A. {task.A}\n"
        f"B. {task.B}\n"
        f"C. {task.C}\n"
        f"D. {task.D}\n"
        "Answer:"
    )
    if demonstration_answer is not None:
        if demonstration_answer not in {"A", "B", "C", "D"}:
            raise ValueError("demonstration answer must be A, B, C, or D")
        return f"{block} {demonstration_answer}"
    return block


def render_official_five_shot_prompt(
    task: PublicChemBench4KTask,
    *,
    category_dev: tuple[PrivateChemBench4KTask, ...],
) -> RenderedChemBench4KPrompt:
    """Render the fixed category-local dev indices 0..4 before one test item."""

    if type(task) is not PublicChemBench4KTask:
        raise TypeError("task must be an exact PublicChemBench4KTask")
    _validate_demonstrations(
        task,
        category_dev=category_dev,
        expected_count=5,
        expected_indices=(0, 1, 2, 3, 4),
    )
    return _render_prompt(task, category_dev=category_dev)


@dataclass(frozen=True, slots=True, repr=False)
class DevLeaveOneOutTask:
    """Private dev evaluation task paired with its answer-safe four-shot prompt."""

    evaluation_task: PrivateChemBench4KTask
    prompt: RenderedChemBench4KPrompt

    def __repr__(self) -> str:
        return "DevLeaveOneOutTask(<redacted>)"

    __str__ = __repr__

    def __post_init__(self) -> None:
        if type(self.evaluation_task) is not PrivateChemBench4KTask:
            raise TypeError("evaluation_task must be a private ChemBench4K task")
        if type(self.prompt) is not RenderedChemBench4KPrompt:
            raise TypeError("prompt must be a rendered ChemBench4K prompt")
        if self.evaluation_task.source_split != "dev":
            raise ValueError("leave-one-out evaluation task must come from dev")
        if self.evaluation_task.uid != self.prompt.uid:
            raise ValueError("leave-one-out task and prompt identities differ")
        if self.evaluation_task.uid in self.prompt.demonstration_uids:
            raise ValueError("leave-one-out prompt contains its evaluation item")


def build_dev_leave_one_out_tasks(
    category_dev: tuple[PrivateChemBench4KTask, ...],
) -> tuple[DevLeaveOneOutTask, ...]:
    """Create one four-shot evaluation for every item in a five-row dev category."""

    if (
        not isinstance(category_dev, tuple)
        or len(category_dev) != 5
        or any(type(task) is not PrivateChemBench4KTask for task in category_dev)
    ):
        raise ValueError("category_dev must contain exactly five private tasks")
    ordered = tuple(sorted(category_dev, key=lambda task: task.source_index))
    if tuple(task.source_index for task in ordered) != (0, 1, 2, 3, 4):
        raise ValueError("dev source indices must be exactly 0..4")
    first = ordered[0]
    if any(
        task.category != first.category
        or task.source_split != "dev"
        or task.dataset_revision != CHEMBENCH4K_REVISION
        or task.dataset_sha256 != first.dataset_sha256
        for task in ordered
    ):
        raise ValueError("leave-one-out tasks must share one frozen dev category")

    result: list[DevLeaveOneOutTask] = []
    for evaluation_task in ordered:
        demonstrations = tuple(
            candidate for candidate in ordered if candidate.uid != evaluation_task.uid
        )
        prompt = _render_prompt(evaluation_task.to_public(), category_dev=demonstrations)
        result.append(
            DevLeaveOneOutTask(
                evaluation_task=evaluation_task,
                prompt=prompt,
            )
        )
    return tuple(result)


def build_all_dev_leave_one_out_tasks(
    dev_by_category: dict[str, tuple[PrivateChemBench4KTask, ...]],
) -> tuple[DevLeaveOneOutTask, ...]:
    """Build the complete 45-item dev-only trajectory in frozen category order."""

    if not isinstance(dev_by_category, dict):
        raise TypeError("dev_by_category must be a dict")
    if set(dev_by_category) != set(CHEMBENCH4K_CATEGORIES):
        raise ValueError("dev_by_category must contain all nine frozen categories")
    return tuple(
        task
        for category in CHEMBENCH4K_CATEGORIES
        for task in build_dev_leave_one_out_tasks(dev_by_category[category])
    )


def _render_prompt(
    task: PublicChemBench4KTask,
    *,
    category_dev: tuple[PrivateChemBench4KTask, ...],
) -> RenderedChemBench4KPrompt:
    blocks = [
        _question_block(demo.to_public(), demonstration_answer=demo.target)
        for demo in category_dev
    ]
    blocks.append(_question_block(task))
    return RenderedChemBench4KPrompt(
        uid=task.uid,
        category=task.category,
        dataset_revision=task.dataset_revision,
        demonstration_uids=tuple(demo.uid for demo in category_dev),
        text="\n\n".join(blocks),
    )


def _validate_demonstrations(
    task: PublicChemBench4KTask,
    *,
    category_dev: tuple[PrivateChemBench4KTask, ...],
    expected_count: int,
    expected_indices: tuple[int, ...],
) -> None:
    if not isinstance(category_dev, tuple) or len(category_dev) != expected_count:
        raise ValueError(f"category_dev must contain exactly {expected_count} tasks")
    if any(type(demo) is not PrivateChemBench4KTask for demo in category_dev):
        raise TypeError("category_dev must contain private ChemBench4K tasks")
    if tuple(demo.source_index for demo in category_dev) != expected_indices:
        raise ValueError("category_dev ordering is not the frozen index order")
    if any(
        demo.source_split != "dev"
        or demo.category != task.category
        or demo.dataset_revision != task.dataset_revision
        for demo in category_dev
    ):
        raise ValueError("demonstrations must be category-local frozen dev tasks")
    if task.uid in {demo.uid for demo in category_dev}:
        raise ValueError("evaluation item cannot be one of its demonstrations")


__all__ = [
    "DevLeaveOneOutTask",
    "OFFICIAL_INSTRUCTION",
    "build_all_dev_leave_one_out_tasks",
    "build_dev_leave_one_out_tasks",
    "render_official_five_shot_prompt",
]
