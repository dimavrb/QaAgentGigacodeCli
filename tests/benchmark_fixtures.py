"""A labelled reasoning CLI fixture; never a live model or semantic oracle."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from run_agent_benchmark import NEEDED_FLAGS, run_benchmark as _run_benchmark


def create_reasoning_cli(path):
    path.write_text('''#!/usr/bin/env python3
import json, sys, os, time
if '--help' in sys.argv:
    print(''' + repr(' '.join(NEEDED_FLAGS)) + ''')
elif '--version' in sys.argv:
    print('fixture-cli-1')
else:
    assert '--bare' in sys.argv and '--exclude-tools' in sys.argv
    assert not os.listdir(os.getcwd())
    prompt = sys.argv[sys.argv.index('-p') + 1]
    assert 'check_id' not in prompt and 'expectation' not in prompt
    if 'SLEEP' in os.environ: time.sleep(float(os.environ['SLEEP']))
    print(json.dumps([{'type': 'result', 'response': 'Учебный ответ без оценки качества',
                      'usage': {'input_tokens': 12, 'output_tokens': 7}}]))
''')
    path.chmod(0o755)
    return path


def run_fixture_benchmark(output, **options):
    """Test captures must remain labelled fixture, including legacy call sites."""
    if options.pop("measurement_kind", "fixture") != "fixture":
        raise ValueError("A fixture CLI cannot produce a live measurement")
    return _run_benchmark(output, measurement_kind="fixture", **options)
