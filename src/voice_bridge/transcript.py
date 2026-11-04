from dataclasses import dataclass

from voice_bridge.providers.base import ProviderError, SegmentOrder, Transcript


@dataclass
class Segment:
    id: str
    text: str = ""
    is_final: bool = False
    revision: int = 0


class TranscriptState:
    """Retain snapshots, with speech order independent of result arrival order."""

    def __init__(self) -> None:
        self.segments: dict[str, Segment] = {}
        self.predecessors: dict[str, str | None] = {}
        self.committed: set[str] = set()

    def _ensure(self, segment_id: str) -> Segment:
        if segment_id not in self.segments:
            self.segments[segment_id] = Segment(segment_id)
        return self.segments[segment_id]

    def order(self, event: SegmentOrder) -> bool:
        before = self.segment_ids()
        if event.segment_id in self.predecessors:
            if self.predecessors[event.segment_id] != event.previous_segment_id:
                raise ProviderError("upstream_protocol_error", "Inconsistent transcript ordering")
        # Validate before changing state so errors can still return confirmed text.
        predecessor = event.previous_segment_id
        seen = {event.segment_id}
        while predecessor is not None:
            if predecessor in seen:
                raise ProviderError("upstream_protocol_error", "Cyclic transcript ordering")
            seen.add(predecessor)
            predecessor = self.predecessors.get(predecessor)
        if event.previous_segment_id:
            self._ensure(event.previous_segment_id)
        self._ensure(event.segment_id)
        self.predecessors[event.segment_id] = event.previous_segment_id
        self.committed.add(event.segment_id)
        return before != self.segment_ids()

    def segment_ids(self) -> list[str]:
        # Topological traversal preserves unknown segments' insertion order and
        # resolves a predecessor even when its own text hasn't arrived yet.
        result: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(segment_id: str) -> None:
            if segment_id in visited:
                return
            if segment_id in visiting:
                raise ProviderError("upstream_protocol_error", "Cyclic transcript ordering")
            visiting.add(segment_id)
            predecessor = self.predecessors.get(segment_id)
            if predecessor:
                visit(predecessor)
            visiting.remove(segment_id)
            visited.add(segment_id)
            result.append(segment_id)

        for segment_id in self.segments:
            visit(segment_id)
        return result

    def apply(self, event: Transcript) -> Segment | None:
        segment = self._ensure(event.segment_id)
        if segment.is_final:
            if event.is_final and event.text != segment.text:
                raise ProviderError("upstream_protocol_error", "Conflicting final transcript")
            return None
        if segment.revision and segment.text == event.text and not event.is_final:
            return None
        segment.text = event.text
        segment.is_final = event.is_final
        segment.revision += 1
        return segment

    def text(self, *, final_only: bool = False) -> str:
        return "".join(
            self.segments[key].text
            for key in self.segment_ids()
            if not final_only or self.segments[key].is_final
        )

    @property
    def has_pending(self) -> bool:
        return any(
            (segment.revision or segment.id in self.committed) and not segment.is_final
            for segment in self.segments.values()
        )
