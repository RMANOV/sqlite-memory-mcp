#!/usr/bin/env python3
"""Runtime resource governor for local debate wake orchestration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class ResourceSnapshot:
    mem_total_mib: int = 0
    mem_available_mib: int = 0
    swap_total_mib: int = 0
    swap_free_mib: int = 0
    cpu_count: int = 1
    load1: float = 0.0
    memory_full_avg10: float = 0.0
    max_temp_c: float | None = None
    live_agent_count: int = 0


@dataclass(frozen=True)
class DebateResourceBudget:
    allow_agent: bool
    wake_budget: int
    max_workers_per_scan: int
    max_concurrent_workers: int
    interval_seconds: float
    limit: int
    action_kinds: tuple[str, ...]
    tier: str
    reason: str
    snapshot: ResourceSnapshot

    def to_dict(self) -> dict[str, object]:
        out = asdict(self)
        out["action_kinds"] = list(self.action_kinds)
        return out


def _read_meminfo(path: Path = Path("/proc/meminfo")) -> tuple[int, int, int, int]:
    data: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, rest = line.partition(":")
            match = re.search(r"\d+", rest)
            if match:
                data[key] = int(match.group(0)) // 1024
    except Exception:
        return 0, 0, 0, 0
    return (
        data.get("MemTotal", 0),
        data.get("MemAvailable", 0),
        data.get("SwapTotal", 0),
        data.get("SwapFree", 0),
    )


def _read_load1(path: Path = Path("/proc/loadavg")) -> float:
    try:
        return float(path.read_text(encoding="utf-8").split()[0])
    except Exception:
        return 0.0


def _read_memory_pressure(path: Path = Path("/proc/pressure/memory")) -> float:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("full "):
                for part in line.split():
                    if part.startswith("avg10="):
                        return float(part.split("=", 1)[1])
    except Exception:
        return 0.0
    return 0.0


def _read_max_temp_c(hwmon_root: Path = Path("/sys/class/hwmon")) -> float | None:
    temps: list[float] = []
    try:
        hwmons = list(hwmon_root.glob("hwmon*"))
    except Exception:
        return None
    for hwmon in hwmons:
        try:
            name_path = hwmon / "name"
            name = name_path.read_text(encoding="utf-8").strip() if name_path.exists() else ""
        except Exception:
            name = ""
        if name not in {"coretemp", "k10temp", "dell_smm", "thinkpad", "acpitz"}:
            continue
        for temp_path in hwmon.glob("temp*_input"):
            try:
                value = int(temp_path.read_text(encoding="utf-8").strip()) / 1000
            except Exception:
                continue
            if 0 < value < 130:
                temps.append(value)
    return max(temps) if temps else None


def _count_live_agents(proc: Path = Path("/proc")) -> int:
    count = 0
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except Exception:
        return 0
    agent_needles = (
        "claude -p",
        "claude",
        "codex exec",
        "/@openai/codex",
    )
    sidecar_needles = (
        "--chrome-native-host",
        "node /home/rmanov/.npm-global/bin/codex",
        "node /home/rmanov/.npm-global/bin/codex-real",
        "sqlite-memory-intel",
        "sqlite-memory-tasks",
        "sqlite-memory-bridge",
        "sqlite-memory-collab",
        "sqlite-memory-entity",
        "sqlite-memory-session",
        "sqlite-memory-core",
    )
    for pid in pids:
        try:
            cmdline = (pid / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", errors="replace"
            )
        except Exception:
            continue
        if any(needle in cmdline for needle in sidecar_needles):
            continue
        if any(needle in cmdline for needle in agent_needles):
            count += 1
    return count


def read_resource_snapshot() -> ResourceSnapshot:
    mem_total, mem_available, swap_total, swap_free = _read_meminfo()
    return ResourceSnapshot(
        mem_total_mib=mem_total,
        mem_available_mib=mem_available,
        swap_total_mib=swap_total,
        swap_free_mib=swap_free,
        cpu_count=max(1, os.cpu_count() or 1),
        load1=_read_load1(),
        memory_full_avg10=_read_memory_pressure(),
        max_temp_c=_read_max_temp_c(),
        live_agent_count=_count_live_agents(),
    )


def compute_debate_resource_budget(snapshot: ResourceSnapshot) -> DebateResourceBudget:
    load_per_cpu = snapshot.load1 / max(1, snapshot.cpu_count)
    temp = snapshot.max_temp_c
    reasons: list[str] = []
    soft_reasons: list[str] = []
    mem_available_pct = (
        snapshot.mem_available_mib / snapshot.mem_total_mib
        if snapshot.mem_total_mib > 0
        else 0.0
    )
    swap_free_pct = (
        snapshot.swap_free_mib / snapshot.swap_total_mib
        if snapshot.swap_total_mib > 0
        else 1.0
    )
    live_agent_critical = max(24, snapshot.cpu_count * 3)
    live_agent_constrained = max(8, snapshot.cpu_count)

    if temp is None:
        soft_reasons.append("temperature_unknown")
    if temp is not None and temp >= 105:
        reasons.append(f"temperature_critical_{temp:.0f}c")
    elif temp is not None and temp >= 90:
        soft_reasons.append(f"temperature_spike_{temp:.0f}c")
    if snapshot.mem_total_mib > 0 and mem_available_pct < 0.08:
        reasons.append(f"mem_available_pct_low_{mem_available_pct:.0%}")
    elif snapshot.mem_total_mib == 0 and snapshot.mem_available_mib < 1024:
        reasons.append(f"mem_available_low_{snapshot.mem_available_mib}mib")
    if (
        snapshot.swap_total_mib > 0
        and swap_free_pct < 0.05
        and mem_available_pct < 0.15
    ):
        reasons.append(f"swap_free_pct_low_{swap_free_pct:.0%}")
    if snapshot.memory_full_avg10 >= 5:
        reasons.append(f"memory_pressure_full_avg10_{snapshot.memory_full_avg10:.1f}")
    if load_per_cpu >= 8:
        reasons.append(f"load_per_cpu_critical_{load_per_cpu:.1f}")
    if snapshot.live_agent_count >= live_agent_critical:
        reasons.append(f"live_agent_count_high_{snapshot.live_agent_count}")

    if reasons:
        return DebateResourceBudget(
            allow_agent=False,
            wake_budget=0,
            max_workers_per_scan=0,
            max_concurrent_workers=0,
            interval_seconds=60,
            limit=0,
            action_kinds=("Q", "DECISION"),
            tier="blocked",
            reason=";".join([*reasons, *soft_reasons]),
            snapshot=snapshot,
        )

    if (
        (temp is not None and temp >= 80)
        or (temp is None)
        or (snapshot.mem_total_mib > 0 and mem_available_pct < 0.18)
        or (
            snapshot.swap_total_mib > 0
            and swap_free_pct < 0.15
            and mem_available_pct < 0.25
        )
        or snapshot.memory_full_avg10 >= 1
        or load_per_cpu >= 3
        or snapshot.live_agent_count >= live_agent_constrained
    ):
        return DebateResourceBudget(
            allow_agent=True,
            wake_budget=1,
            max_workers_per_scan=1,
            max_concurrent_workers=1,
            interval_seconds=30,
            limit=3,
            action_kinds=("Q", "DECISION"),
            tier="low",
            reason=";".join(soft_reasons) or "constrained_machine_state",
            snapshot=snapshot,
        )

    if (
        (temp is not None and temp < 70)
        and snapshot.mem_available_mib >= 12288
        and snapshot.swap_free_mib >= 4096
        and snapshot.memory_full_avg10 < 0.5
        and load_per_cpu < 1.5
        and snapshot.live_agent_count < 4
    ):
        return DebateResourceBudget(
            allow_agent=True,
            wake_budget=2,
            max_workers_per_scan=2,
            max_concurrent_workers=2,
            interval_seconds=10,
            limit=10,
            action_kinds=("Q", "DECISION", "A"),
            tier="normal",
            reason="healthy_machine_state",
            snapshot=snapshot,
        )

    return DebateResourceBudget(
        allow_agent=True,
        wake_budget=1,
        max_workers_per_scan=1,
        max_concurrent_workers=1,
        interval_seconds=15,
        limit=5,
        action_kinds=("Q", "DECISION"),
        tier="guarded",
        reason="moderate_machine_state",
        snapshot=snapshot,
    )


def current_debate_resource_budget() -> DebateResourceBudget:
    budget = compute_debate_resource_budget(read_resource_snapshot())
    if os.environ.get("DEBATE_RESOURCE_HYSTERESIS", "on") == "off":
        return apply_operator_sleep(budget)
    state_path = Path(
        os.environ.get(
            "DEBATE_RESOURCE_BUDGET_STATE",
            os.path.expanduser("~/.claude/memory/debate_resource_budget_state.json"),
        )
    )
    return apply_operator_sleep(apply_recovery_hysteresis(budget, state_path=state_path))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_sleep_until(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    if value.startswith("{"):
        try:
            payload = json.loads(value)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        raw_until = payload.get("until") or payload.get("sleep_until")
        value = str(raw_until or "").strip()
        if not value:
            return None
    else:
        value = value.splitlines()[0].strip()

    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except ValueError:
        pass

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed.astimezone(timezone.utc)


def _sleep_until_file() -> Path:
    return Path(
        os.environ.get(
            "DEBATE_WAKE_SLEEP_UNTIL_FILE",
            os.path.expanduser("~/.claude/memory/debate_wake.sleep_until"),
        )
    )


def apply_operator_sleep(
    budget: DebateResourceBudget,
    *,
    path: Path | None = None,
    now: datetime | None = None,
) -> DebateResourceBudget:
    path = path or _sleep_until_file()
    if now is None:
        now = datetime.now(timezone.utc)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return budget
    except Exception:
        return DebateResourceBudget(
            allow_agent=False,
            wake_budget=0,
            max_workers_per_scan=0,
            max_concurrent_workers=0,
            interval_seconds=60,
            limit=0,
            action_kinds=budget.action_kinds,
            tier="sleep",
            reason=f"operator_sleep_until_unreadable:{path}",
            snapshot=budget.snapshot,
        )

    until = _parse_sleep_until(raw)
    if until is None:
        return DebateResourceBudget(
            allow_agent=False,
            wake_budget=0,
            max_workers_per_scan=0,
            max_concurrent_workers=0,
            interval_seconds=60,
            limit=0,
            action_kinds=budget.action_kinds,
            tier="sleep",
            reason=f"operator_sleep_until_invalid:{path}",
            snapshot=budget.snapshot,
        )

    if until <= now:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            return DebateResourceBudget(
                allow_agent=False,
                wake_budget=0,
                max_workers_per_scan=0,
                max_concurrent_workers=0,
                interval_seconds=60,
                limit=0,
                action_kinds=budget.action_kinds,
                tier="sleep",
                reason=f"operator_sleep_until_expired_unlink_failed:{path}",
                snapshot=budget.snapshot,
            )
        return budget

    seconds = max(1.0, (until - now).total_seconds())
    reason = "operator_sleep_until_" + until.isoformat().replace("+00:00", "Z")
    if not budget.allow_agent:
        reason = f"{reason};underlying={budget.reason}"
    return DebateResourceBudget(
        allow_agent=False,
        wake_budget=0,
        max_workers_per_scan=0,
        max_concurrent_workers=0,
        interval_seconds=min(60.0, seconds),
        limit=0,
        action_kinds=budget.action_kinds,
        tier="sleep",
        reason=reason,
        snapshot=budget.snapshot,
    )


def _load_state(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def apply_recovery_hysteresis(
    budget: DebateResourceBudget,
    *,
    state_path: Path,
    required_healthy_samples: int = 3,
    temp_alpha: float = 0.2,
    min_temp_samples: int = 5,
    temp_block_c: float = 96.0,
) -> DebateResourceBudget:
    """Require repeated healthy samples before leaving a blocked state."""
    if required_healthy_samples <= 1:
        return budget
    state = _load_state(state_path)
    previous_blocked = bool(state.get("blocked"))
    healthy_streak = int(state.get("healthy_streak") or 0)
    now = _utc_now()
    temp = budget.snapshot.max_temp_c
    previous_ewma = state.get("temp_ewma_c")
    try:
        temp_ewma = float(previous_ewma)
    except (TypeError, ValueError):
        temp_ewma = float(temp) if temp is not None else 0.0
    temp_sample_count = int(state.get("temp_sample_count") or 0)
    if temp is not None:
        alpha = min(1.0, max(0.01, float(temp_alpha)))
        if temp_sample_count <= 0:
            temp_ewma = float(temp)
        else:
            temp_ewma = alpha * float(temp) + (1.0 - alpha) * temp_ewma
        temp_sample_count += 1
    sustained_hot = (
        temp is not None
        and temp_sample_count >= min_temp_samples
        and temp_ewma >= temp_block_c
    )

    if not budget.allow_agent:
        _write_state(
            state_path,
            {
                "blocked": True,
                "healthy_streak": 0,
                "temp_ewma_c": round(temp_ewma, 2) if temp is not None else previous_ewma,
                "temp_sample_count": temp_sample_count,
                "tier": budget.tier,
                "reason": budget.reason,
                "updated_at": now,
            },
        )
        return budget

    if sustained_hot:
        _write_state(
            state_path,
            {
                "blocked": True,
                "healthy_streak": 0,
                "temp_ewma_c": round(temp_ewma, 2),
                "temp_sample_count": temp_sample_count,
                "tier": "blocked",
                "reason": f"sustained_temperature_ewma_{temp_ewma:.0f}c",
                "updated_at": now,
            },
        )
        return DebateResourceBudget(
            allow_agent=False,
            wake_budget=0,
            max_workers_per_scan=0,
            max_concurrent_workers=0,
            interval_seconds=60,
            limit=0,
            action_kinds=budget.action_kinds,
            tier="blocked",
            reason=f"sustained_temperature_ewma_{temp_ewma:.0f}c",
            snapshot=budget.snapshot,
        )

    if not previous_blocked:
        _write_state(
            state_path,
            {
                "blocked": False,
                "healthy_streak": required_healthy_samples,
                "temp_ewma_c": round(temp_ewma, 2) if temp is not None else previous_ewma,
                "temp_sample_count": temp_sample_count,
                "tier": budget.tier,
                "reason": budget.reason,
                "updated_at": now,
            },
        )
        return budget

    healthy_streak += 1
    if healthy_streak < required_healthy_samples:
        _write_state(
            state_path,
            {
                "blocked": True,
                "healthy_streak": healthy_streak,
                "temp_ewma_c": round(temp_ewma, 2) if temp is not None else previous_ewma,
                "temp_sample_count": temp_sample_count,
                "tier": "blocked",
                "reason": f"recovery_hysteresis_{healthy_streak}_of_{required_healthy_samples}",
                "updated_at": now,
            },
        )
        return DebateResourceBudget(
            allow_agent=False,
            wake_budget=0,
            max_workers_per_scan=0,
            max_concurrent_workers=0,
            interval_seconds=max(30, budget.interval_seconds),
            limit=0,
            action_kinds=budget.action_kinds,
            tier="blocked",
            reason=f"recovery_hysteresis_{healthy_streak}_of_{required_healthy_samples}",
            snapshot=budget.snapshot,
        )

    _write_state(
        state_path,
        {
            "blocked": False,
            "healthy_streak": healthy_streak,
            "temp_ewma_c": round(temp_ewma, 2) if temp is not None else previous_ewma,
            "temp_sample_count": temp_sample_count,
            "tier": budget.tier,
            "reason": budget.reason,
            "updated_at": now,
        },
    )
    return budget


def main() -> int:
    print(json.dumps(current_debate_resource_budget().to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
