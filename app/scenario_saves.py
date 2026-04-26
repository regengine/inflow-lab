from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Iterable

from .models import ScenarioSaveSnapshot, SimulationConfig, StoredEventRecord
from .scenarios import ScenarioId


class ScenarioSaveStore:
    def __init__(self, save_dir: str = "data/scenario_saves") -> None:
        self.save_dir = Path(save_dir)
        self._lock = RLock()
        self.save_dir.mkdir(parents=True, exist_ok=True)

    def configure(self, save_dir: str) -> None:
        with self._lock:
            self.save_dir = Path(save_dir)
            self.save_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[ScenarioSaveSnapshot]:
        with self._lock:
            saves = []
            for scenario in ScenarioId:
                snapshot = self.get(scenario)
                if snapshot is not None:
                    saves.append(snapshot)
            return saves

    def get(self, scenario: ScenarioId | str) -> ScenarioSaveSnapshot | None:
        path = self._path_for(scenario)
        with self._lock:
            if not path.exists():
                return None
            return ScenarioSaveSnapshot.model_validate_json(path.read_text(encoding="utf-8"))

    def save(
        self,
        snapshot: ScenarioSaveSnapshot,
    ) -> ScenarioSaveSnapshot:
        with self._lock:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            path = self._path_for(snapshot.scenario)
            tmp_path = path.with_suffix(f"{path.suffix}.tmp")
            tmp_path.write_text(
                json.dumps(snapshot.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
            tmp_path.replace(path)
        return snapshot

    def save_snapshot(
        self,
        scenario: ScenarioId,
        config: SimulationConfig,
        records: Iterable[StoredEventRecord],
    ) -> ScenarioSaveSnapshot:
        return self.save(
            ScenarioSaveSnapshot(
                scenario=scenario,
                config=config,
                records=list(records),
            )
        )

    def _path_for(self, scenario: ScenarioId | str) -> Path:
        normalized = ScenarioId(scenario)
        return self.save_dir / f"{normalized.value}.json"
