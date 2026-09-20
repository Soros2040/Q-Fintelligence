"""Versioned configuration shared by the installed CLI and Python callers."""
from dataclasses import asdict, dataclass, fields
import json
import math
from numbers import Real
from pathlib import Path


def validate_compute_parameters(risk_lambda, cost, qaoa_gamma, qaoa_beta, qaoa_mode):
    """Validate public input angles; optimizer iterates retain their full domain."""
    for name, value, lower, upper in [('RISK_LAMBDA', risk_lambda, 0, 100),
                                      ('COST', cost, 0, .01),
                                      ('QAOA_GAMMA', qaoa_gamma, -math.pi, math.pi),
                                      ('QAOA_BETA', qaoa_beta, -math.pi, math.pi)]:
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or not lower <= value <= upper:
            raise ValueError('CONFIG_' + name + '_RANGE')
    if not isinstance(qaoa_mode, str) or qaoa_mode not in {'fixed', 'optimize'}:
        raise ValueError('CONFIG_QAOA_MODE')


@dataclass(frozen=True)
class RunConfig:
    input_data: object = None
    data_root: object = None
    frozen: bool = False
    output_dir: object = None
    run_id: str | None = None
    seed: int = 2026090902
    shots: int = 1024
    epochs: int = 40
    risk_mode: str = 'factor'
    risk_lambda: float = 1.
    cost: float = .001
    qaoa_gamma: float = .6
    qaoa_beta: float = .25
    qaoa_mode: str = 'optimize'

    def validate(self):
        if type(self.seed) is not int or not 0 <= self.seed < 2**32:
            raise ValueError('CONFIG_UNSIGNED_32BIT_SEED')
        if type(self.shots) is not int or not 1 <= self.shots <= 100000:
            raise ValueError('CONFIG_SHOTS_RANGE')
        if type(self.epochs) is not int or not 1 <= self.epochs <= 200:
            raise ValueError('CONFIG_EPOCHS_RANGE')
        if type(self.frozen) is not bool or (self.frozen and self.input_data is not None):
            raise ValueError('CONFIG_INPUT_MODE')
        if self.risk_mode not in {'factor', 'shrinkage', 'quantum_specific'}:
            raise ValueError('CONFIG_RISK_MODE')
        validate_compute_parameters(self.risk_lambda, self.cost, self.qaoa_gamma, self.qaoa_beta, self.qaoa_mode)
        return self

    def as_kwargs(self):
        return asdict(self.validate())

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, dict):
            raise ValueError('CONFIG_OBJECT_REQUIRED')
        value = dict(value)
        version = value.pop('schemaVersion', 'qf.run-config.v2')
        if version != 'qf.run-config.v2' or set(value)-{f.name for f in fields(cls)}:
            raise ValueError('CONFIG_SCHEMA_OR_UNKNOWN_FIELDS')
        return cls(**value).validate()

    @classmethod
    def from_json(cls, path):
        return cls.from_mapping(json.loads(Path(path).read_text(encoding='utf-8')))
