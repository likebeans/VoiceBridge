"""Minimal Prometheus-style metrics.

Everything runs on one event loop, so plain dictionaries are safe without locks. Label values
must come from small closed sets (provider ids, outcome codes defined by this service); never
pass user input or upstream text, or the series count grows without bound.
"""

import bisect
from collections.abc import Iterable, Sequence

LabelValues = tuple[str, ...]

# Seconds. Covers sub-100 ms handshakes up to the ~135 s session ceiling.
LATENCY_BUCKETS: tuple[float, ...] = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_labels(names: Sequence[str], values: LabelValues, extra: str = "") -> str:
    pairs = [f'{name}="{_escape(value)}"' for name, value in zip(names, values, strict=True)]
    if extra:
        pairs.append(extra)
    return "{" + ",".join(pairs) + "}" if pairs else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        self.name = name
        self.help = help_text
        self.label_names = tuple(labels)

    def _key(self, labels: Sequence[str]) -> LabelValues:
        if len(labels) != len(self.label_names):
            raise ValueError(f"{self.name} expects labels {self.label_names}")
        return tuple(labels)

    def render(self) -> Iterable[str]:
        yield f"# HELP {self.name} {self.help}"
        yield f"# TYPE {self.name} {self.kind}"
        yield from self._samples()

    def _samples(self) -> Iterable[str]:
        raise NotImplementedError


class Counter(_Metric):
    kind = "counter"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[LabelValues, float] = {}

    def inc(self, *labels: str, amount: float = 1) -> None:
        if amount < 0:
            raise ValueError("A counter cannot decrease")
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def value(self, *labels: str) -> float:
        return self._values.get(self._key(labels), 0)

    def _samples(self) -> Iterable[str]:
        for key, value in sorted(self._values.items()):
            yield f"{self.name}{_format_labels(self.label_names, key)} {value:g}"


class Gauge(_Metric):
    kind = "gauge"

    def __init__(self, name: str, help_text: str, labels: Sequence[str] = ()) -> None:
        super().__init__(name, help_text, labels)
        self._values: dict[LabelValues, float] = {}

    def set(self, value: float, *labels: str) -> None:
        self._values[self._key(labels)] = value

    def inc(self, *labels: str, amount: float = 1) -> None:
        key = self._key(labels)
        self._values[key] = self._values.get(key, 0) + amount

    def dec(self, *labels: str, amount: float = 1) -> None:
        self.inc(*labels, amount=-amount)

    def value(self, *labels: str) -> float:
        return self._values.get(self._key(labels), 0)

    def _samples(self) -> Iterable[str]:
        for key, value in sorted(self._values.items()):
            yield f"{self.name}{_format_labels(self.label_names, key)} {value:g}"
        if not self._values and not self.label_names:
            yield f"{self.name} 0"


class Histogram(_Metric):
    kind = "histogram"

    def __init__(
        self,
        name: str,
        help_text: str,
        labels: Sequence[str] = (),
        buckets: Sequence[float] = LATENCY_BUCKETS,
    ) -> None:
        super().__init__(name, help_text, labels)
        self.buckets = tuple(sorted(buckets))
        # Per label set: non-cumulative bucket counts (last slot is +Inf), sum, count.
        self._series: dict[LabelValues, tuple[list[int], list[float]]] = {}

    def observe(self, value: float, *labels: str) -> None:
        key = self._key(labels)
        counts, totals = self._series.setdefault(key, ([0] * (len(self.buckets) + 1), [0.0, 0.0]))
        counts[bisect.bisect_left(self.buckets, value)] += 1
        totals[0] += value
        totals[1] += 1

    def count(self, *labels: str) -> int:
        series = self._series.get(self._key(labels))
        return int(series[1][1]) if series else 0

    def _samples(self) -> Iterable[str]:
        for key, (counts, (total, count)) in sorted(self._series.items()):
            cumulative = 0
            for bound, bucket_count in zip((*self.buckets, float("inf")), counts, strict=True):
                cumulative += bucket_count
                le = "+Inf" if bound == float("inf") else f"{bound:g}"
                labels = _format_labels(self.label_names, key, f'le="{le}"')
                yield f"{self.name}_bucket{labels} {cumulative}"
            labels = _format_labels(self.label_names, key)
            yield f"{self.name}_sum{labels} {total:g}"
            yield f"{self.name}_count{labels} {count:g}"


class Metrics:
    """All service metrics. One instance per app so tests and instances never share state."""

    def __init__(self) -> None:
        self.active_sessions = Gauge(
            "asr_active_sessions", "WebSocket connections currently held by this process."
        )
        self.draining = Gauge(
            "asr_draining", "1 while the process is shutting down and refusing new sessions."
        )
        self.sessions = Counter(
            "asr_sessions_total",
            "Finished sessions by provider and outcome (done, or the error code).",
            ("provider", "outcome"),
        )
        self.close_codes = Counter(
            "asr_close_codes_total",
            "Session close codes (private codes grouped as other).",
            ("provider", "code"),
        )
        self.upstream_open_seconds = Histogram(
            "asr_upstream_open_seconds",
            "Duration of each upstream open attempt including cleanup.",
            ("provider",),
        )
        self.rejected = Counter(
            "asr_sessions_rejected_total",
            "Connections turned away before a session started.",
            ("reason",),
        )
        self.audio_seconds = Counter(
            "asr_audio_seconds_total", "Audio forwarded upstream, in seconds.", ("provider",)
        )
        self.upstream_open = Counter(
            "asr_upstream_open_total",
            "Upstream connection attempts by provider and result (ok, or the error code).",
            ("provider", "result"),
        )
        self.upstream_retries = Counter(
            "asr_upstream_retries_total", "Same-provider connection retries.", ("provider",)
        )
        self.fallbacks = Counter(
            "asr_provider_fallbacks_total",
            "Sessions moved to another provider before starting.",
            ("from_provider", "to_provider"),
        )
        self.ready_seconds = Histogram(
            "asr_ready_seconds",
            "Time from the start message to ready, including the upstream handshake.",
            ("provider",),
        )
        self.first_transcript_seconds = Histogram(
            "asr_first_transcript_seconds",
            "Time from the first valid audio received to the first non-empty transcript."
            " Includes any leading silence, so read it as a distribution, not a promise.",
            ("provider",),
        )
        self.finish_seconds = Histogram(
            "asr_finish_seconds",
            "Time from the stop message to done.",
            ("provider",),
        )
        self.session_seconds = Histogram(
            "asr_session_seconds",
            "Total session duration.",
            ("provider",),
            buckets=(1, 2, 5, 10, 20, 30, 60, 90, 120, 150),
        )
        self._all = (
            self.active_sessions,
            self.draining,
            self.sessions,
            self.close_codes,
            self.upstream_open_seconds,
            self.rejected,
            self.audio_seconds,
            self.upstream_open,
            self.upstream_retries,
            self.fallbacks,
            self.ready_seconds,
            self.first_transcript_seconds,
            self.finish_seconds,
            self.session_seconds,
        )

    def render(self) -> str:
        lines = [line for metric in self._all for line in metric.render()]
        return "\n".join(lines) + "\n"
