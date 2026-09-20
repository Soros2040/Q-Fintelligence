"""Public calculation interface for QF Algorithm 2.1.0."""
__version__ = '2.1.0'
from .api import ResultBundle, run_topic, verify
from .paths import DataLayout
from .pipeline import run_pipeline
from .data import load_panel, validate_panel
from .selection import LearningProxy
from .config import RunConfig

__all__ = ['__version__', 'ResultBundle', 'run_topic', 'verify', 'DataLayout', 'run_pipeline', 'load_panel', 'validate_panel', 'LearningProxy', 'RunConfig']
