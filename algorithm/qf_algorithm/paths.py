"""Resolve data by its manifest layout; never by a machine-specific checkout."""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path

CAMPAIGN = 'e01_e07_remote_20260907T160709Z'
PACKAGE = Path(__file__).resolve().parent


def default_root():
    if os.environ.get('QF_DATA_ROOT'):
        return Path(os.environ['QF_DATA_ROOT']).expanduser().resolve()
    for start in (Path.cwd(), PACKAGE):
        for root in (start, *start.parents):
            if (root / 'data/campaign/quantum_freeze.json').is_file():
                return root / 'data'
            if (root / '04_experiments/runs' / CAMPAIGN / 'experiment_registry.json').is_file():
                return root
    raise FileNotFoundError('Set QF_DATA_ROOT or --data-root to the archive or complete distribution root')


@dataclass(frozen=True)
class DataLayout:
    root: Path
    campaign: Path
    registry: Path
    coverage: Path
    d02: Path
    hardware: Path
    configuration: Path

    @classmethod
    def resolve(cls, value=None):
        root = Path(value).expanduser().resolve() if value is not None else default_root()
        workspace = root
        if (root / 'data/campaign/quantum_freeze.json').is_file():
            root = root / 'data'
        if (root / 'campaign/quantum_freeze.json').is_file():
            if (root.parent / '03_data/ready/d02/core6_model_inputs').is_dir():
                workspace = root.parent
            return cls(root, root / 'campaign', root / 'experiment_registry.json', root / 'coverage_manifest.json',
                       workspace / '03_data/ready/d02/core6_model_inputs', root / 'hardware_matched', root / 'campaign_configuration.json')
        if (root / '04_experiments/runs' / CAMPAIGN).is_dir():
            run = root / '04_experiments/runs' / CAMPAIGN
        elif (root / 'experiment_registry.json').is_file() and (root / 'remote_results').is_dir():
            run = root
            workspace = root.parents[2]
        elif root.name == 'remote_results' and (root.parent / 'experiment_registry.json').is_file():
            run = root.parent
            workspace = run.parents[2]
        else:
            raise FileNotFoundError('Unsupported data layout: expected campaign/ or 04_experiments/runs/')
        return cls(workspace, run / 'remote_results', run / 'experiment_registry.json',
                   run / 'processed_rebuild/coverage_manifest.json', workspace / '03_data/ready/d02/core6_model_inputs',
                   run.parent / 'hardware_matched_reference_20260908_v1', run / 'campaign.json')

    def require(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError('Archive input missing or outside data root: ' + str(path))
        return path

    def protected(self, output):
        output = Path(output).resolve()
        return any(output.is_relative_to(p) for p in (self.campaign, self.d02, self.hardware, PACKAGE))
