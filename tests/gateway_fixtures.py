"""Synthetic scoped ticket service shared by gateway and managed tests."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

SERVER = ROOT / "tests/fixtures/action_server.py"


def policy(store, disconnect=False):
    return dict(schema_version=1, server_command=[sys.executable, str(SERVER), '--store', str(store)] +
                (['--disconnect-on-create'] if disconnect else []), call_timeout_seconds=1,
                max_write_actions=2, max_action_attempts=2,
                tools={'create_ticket': dict(effect='write', constraints={'/project':'SYNTHETIC'},
                    allowed_arguments=['project','title'], idempotency_argument='client_id',
                    readback=dict(tool='find_ticket', arguments={'project':{'literal':'SYNTHETIC'},'client_id':{'action_key':True}},
                                  presence_pointer='/found', authoritative_absence=True)),
                       'find_ticket':dict(effect='read',constraints={'/project':'SYNTHETIC'},allowed_arguments=['project','client_id'])})
