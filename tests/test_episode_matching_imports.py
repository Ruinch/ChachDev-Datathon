"""Fresh-process import checks; no videos, network requests or model runtime."""
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize('first', ['app.ml.episode_matching', 'app.local_analyzer'])
def test_application_imports_work_without_research_package_or_import_cycle(first):
    script = '''
import importlib
import importlib.abc
import sys

class BlockResearch(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'ml_tools' or fullname.startswith('ml_tools.'):
            raise ImportError('Research modules must not be a service dependency')

sys.meta_path.insert(0, BlockResearch())
importlib.import_module(sys.argv[1])
matcher = importlib.import_module('app.ml.episode_matching')
analyzer = importlib.import_module('app.local_analyzer')
assert matcher.same_episode.__module__ == 'app.ml.episode_matching'
assert analyzer.build_analyzer().candidate_matcher is matcher.same_episode
assert not any(name == 'ml_tools' or name.startswith('ml_tools.') for name in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, '-c', script, first], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    assert result.returncode == 0, result.stderr


def test_research_shim_reexports_exact_application_functions():
    from app.ml import episode_matching as application
    from ml_tools import episode_matching as research

    assert research.same_episode is application.same_episode
    assert research.admissible_trigger_ranges is application.admissible_trigger_ranges
    assert research.MATCHING_POLICY == application.MATCHING_POLICY
