#!/usr/bin/env python3
"""Synthetic Jira receipts; no Jira, installed skill or live model is used."""
import argparse
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests')]
import task_exchange as exchange
from action_gateway import ActionGateway
from investigation_state import result_digest
from self_review_fixtures import review_for


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SyntheticJira:
    def __init__(self, uncertain=False):
        self.uncertain = uncertain
        self.writes = 0

    def request(self, method, params):
        if params['name'] == 'create_issue':
            self.writes += 1
            if self.uncertain:
                raise TimeoutError('Synthetic timeout after simulated application')
        return dict(structuredContent=dict(present=True, key='FIXTURE-1'))


def build(output, *, duplicate=False, uncertain=False):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    investigation = output / 'investigation'
    shutil.copytree(ROOT / 'examples/investigations/confirmed', investigation)
    shutil.copyfile(ROOT / 'examples/investigations/requirements.md', output / 'requirements.md')
    result = exchange.read_json(investigation / 'result.json')
    result['self_review'] = review_for()
    state = exchange.read_json(investigation / 'state.json')
    state['review']['result_sha256'] = result_digest(result)  # Fresh runtime copy, never the archived fixture.
    exchange.atomic_write(investigation / 'result.json', result)
    exchange.atomic_write(investigation / 'state.json', state)
    registry = output / 'task.json'
    exchange.init_task(registry, 'QA-FIXTURE', 'Synthetic Jira publication')
    author = dict(task_id='QA-FIXTURE', request_id=result['request_id'], agent='qa-defect-investigator',
        stage='investigation', goal='Read the synthetic saved observation', acceptance_criteria=['Evidence supports the fixture verdict'],
        stop_condition='Confirmed observation or blocker', expected_artifact='QA JSON',
        result_path=str(investigation / 'result.json'), investigation_state=str(investigation / 'state.json'),
        inputs=[str(output / 'requirements.md')], depends_on=[])
    exchange.assign(registry, author, output, None)
    exchange.prepare_handoff(registry, author['request_id'], None)
    exchange.submit_result(registry, author['request_id'], None)
    exchange.review_result(registry, author['request_id'], 'Synthetic investigation materials read', True, None)
    skill = output / 'synthetic-skill.md'
    skill.write_text('Synthetic skill placeholder; not an installed jira-defect-creator skill. No Jira operations.\n')
    fields = dict(project_key='FIXTURE', issue_type='Bug', summary='Synthetic unauthorized user list',
                  description='Synthetic buyer receives HTTP 200 and users instead of expected HTTP 403. Fixture only; cause not asserted.')
    arguments = dict(project='FIXTURE', summary=fields['summary'], description=fields['description'])
    policy = dict(schema_version=1, server_command=['synthetic-no-server'], max_write_actions=1, max_action_attempts=1,
        tools=dict(create_issue=dict(effect='write', allowed_arguments=list(arguments),
            constraints={'/' + k: v for k, v in arguments.items()},
            readback=dict(tool='read_issue', arguments=dict(key=dict(literal='FIXTURE-1')),
                          presence_pointer='/present', authoritative_absence=False)),
            read_issue=dict(effect='read', allowed_arguments=['key'], constraints={'/key': 'FIXTURE-1'})))
    exchange.atomic_write(output / 'policy.json', policy)
    scope = dict(schema_version=1, task_id='QA-FIXTURE', investigation_request_id=author['request_id'],
        investigation_sha256=digest(investigation / 'result.json'), created_at=exchange.now(),
        jira_base_url='https://jira.example.invalid', project_key='FIXTURE', expected_fields=fields,
        skill=dict(path=str(skill), sha256=digest(skill)),
        gateway=dict(state_path=str(output / 'gateway.json'), policy_path=str(output / 'policy.json'),
            step_id='jira-create', create_tool='create_issue', create_arguments=arguments,
            issue_key_pointer='/structuredContent/key'))
    exchange.atomic_write(output / 'jira-assignment.json', scope)
    client = SyntheticJira(uncertain)
    gateway = ActionGateway(output / 'gateway.json', policy, 'QA-FIXTURE', 'jira-create', client)
    key = 'FIXTURE-7' if duplicate else 'FIXTURE-1'
    url = scope['jira_base_url'] + '/browse/' + key
    candidates = []
    if duplicate:
        exchange.atomic_write(output / 'candidate.json', dict(observed_at=exchange.now(),
            jira_base_url=scope['jira_base_url'], issue_key=key, issue_url=url, fields=fields))
        candidates.append(dict(issue_key=key, issue_url=url, assessment='same_defect',
            reason='Synthetic issue describes the same expected HTTP 403 versus actual 200 for buyer',
            readback_artifact='candidate.json'))
    exchange.atomic_write(output / 'search.json', dict(observed_at=exchange.now(), jira_base_url=scope['jira_base_url'],
        project_key='FIXTURE', query='Synthetic symptom/build/component search; no Jira called', complete=True, candidates=candidates))
    if not duplicate:
        try:
            gateway.call('create_issue', arguments)
        except TimeoutError:
            pass
    exchange.atomic_write(output / 'readback.json', dict(observed_at=exchange.now(),
        jira_base_url=scope['jira_base_url'], issue_key=key, issue_url=url, fields=fields))
    exchange.atomic_write(output / 'publication.json', dict(schema_version=1, task_id='QA-FIXTURE', request_id='jira-1',
        search_artifact='search.json', readback_artifact='readback.json'))
    assignment = dict(task_id='QA-FIXTURE', request_id='jira-1', agent='qa-jira-defect-creator', stage='jira',
        goal='Check synthetic publication protocol', acceptance_criteria=['Accepted source, duplicate check and saved fields'],
        stop_condition='Verified artifact or blocker', expected_artifact='QA JSON', result_path=str(output / 'jira-result.json'),
        inputs=[], depends_on=[author['request_id']], jira_assignment=str(output / 'jira-assignment.json'))
    message = dict(task_id='QA-FIXTURE', request_id='jira-1', agent='qa-jira-defect-creator',
        event='jira_duplicate_found' if duplicate else 'jira_defect_created', summary='Synthetic publication only; no Jira task created',
        artifacts=[], blockers=[], self_review=review_for(), payload=dict(issue_key=key, issue_url=url,
            duplicate_check='Synthetic search candidates assessed', jira_assignment_sha256=digest(output / 'jira-assignment.json'),
            publication_artifact='publication.json', evidence=[str(investigation / 'observation.txt')]))
    exchange.atomic_write(output / 'assignment.json', assignment)
    exchange.atomic_write(output / 'jira-result.json', message)
    return output, client, gateway


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path)
    parser.add_argument('--duplicate', action='store_true')
    args = parser.parse_args()
    print(build(args.output_dir, duplicate=args.duplicate)[0])
