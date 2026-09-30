"""Run isolated, explicitly labelled AOS capability evidence pools."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'contracts': {
        'scope': 'Contract, policy, persistence and negative tests; not real-model proof',
        'tests': [], 'flags': [], 'allow_skips': True},
    'ui': {
        'scope': 'Real Chromium/Docker UI; synthetic decision engines and route fixtures',
        'tests': ['test_development_ui', 'test_first_use_ui', 'test_auto_approval_ui',
                  'test_desktop_mcp_ui', 'test_owned_skill_release_ui', 'test_ui_language_contract',
                  'test_capability_evidence_ui', 'test_failure_improvement_ui',
                  'test_failure_followup_ui', 'test_failure_guidance_ui', 'test_hello_guidance_reuse_ui',
                  'test_knowledge_ui', 'test_knowledge_answer_console.KnowledgeAnswerRenderedTests',
                  'test_task_knowledge_ui', 'test_task_knowledge_report_ui', 'test_local_ui_login_ui',
                  'test_owned_skill_knowledge_ui'],
        'flags': ['AOS_DESKTOP_TESTS', 'AOS_UI_TESTS', 'AOS_DESKTOP_MCP_TESTS']},
    'transport': {
        'scope': 'Real headless/visible Chromium DOM/vision/MCP and rejection paths; fixture decision engines',
        'tests': ['test_browser.BrowserIntegrationTests', 'test_vision.VisionIntegrationTests',
                  'test_desktop_browser.DesktopBrowserIntegrationTests',
                  'test_desktop_vision.DesktopVisionIntegrationTests',
                  'test_desktop_mcp.MCPBrowserIntegrationTests'],
        'flags': ['AOS_BROWSER_TESTS', 'AOS_DESKTOP_TESTS', 'AOS_DESKTOP_MCP_TESTS']},
    'real_tasks': {
        'scope': 'Real Decider/Bonsai: file, headless/visible form and SAVE, pause and model reuse; synthetic tasks',
        'tests': ['test_desktop_task_ui.RealBrowserTaskTests',
                  'test_visible_scheduler.VisibleSchedulerTests.test_real_bonsai_decider_visible_capture_and_independent_verification',
                  'test_visible_scheduler.VisibleSchedulerTests.test_real_decider_visible_form_with_independent_verification',
                  'test_visible_scheduler.VisibleSchedulerTests.test_real_decider_cpu_ready_form_baseline',
                  'test_visible_scheduler.VisibleSchedulerTests.test_reusable_real_decider_two_fresh_decisions_and_job_release',
                  'test_failure_followup_real.FailureFollowupRealTests',
                  'test_failure_guidance_real.FailureGuidanceRealTests',
                  'test_knowledge_answer_real.KnowledgeAnswerRealTests',
                  'test_task_knowledge_real.TaskKnowledgeRealTests',
                  'test_owned_skill_knowledge_real.OwnedSkillKnowledgeRealTests',
                  'test_web_goal_planner_real.WebGoalPlannerRealTests'],
        'flags': ['AOS_DESKTOP_TESTS', 'AOS_REAL_BROWSER_TASK_TESTS', 'AOS_FAILURE_FOLLOWUP_REAL_TESTS',
                  'AOS_FAILURE_GUIDANCE_REAL_TESTS', 'AOS_KNOWLEDGE_ANSWER_REAL_TESTS', 'AOS_TASK_KNOWLEDGE_REAL_TESTS',
                  'AOS_OWNED_SKILL_KNOWLEDGE_REAL_TESTS', 'AOS_WEB_GOAL_PLANNER_REAL_TESTS']},
    'real_mcp': {
        'scope': 'Real Decider and visible Ubuntu Chromium/Playwright MCP; navigation, profile pins and page evidence',
        'tests': ['test_desktop_mcp.MCPSchedulerIntegrationTests.test_real_decider_local_navigation_with_independent_verification',
                  'test_desktop_mcp.MCPSchedulerIntegrationTests.test_real_decider_with_opt_in_synthetic_profile_pin',
                  'test_desktop_mcp.MCPSchedulerIntegrationTests.test_two_profile_pinned_runs_yield_local_bound_page_candidate'],
        'flags': ['AOS_DESKTOP_TESTS', 'AOS_DESKTOP_MCP_TESTS', 'AOS_REAL_BROWSER_TASK_TESTS']},
    'real_takeover': {
        'scope': 'Real Decider cancellation, fresh approval, GPU residency/expiry and release before Bonsai',
        'tests': ['test_desktop_task_ui.RealDesktopTaskTests'],
        'flags': ['AOS_REAL_TASK_TESTS']},
    'real_reuse': {
        'scope': 'Real Decider selected-skill reuse and Bonsai document-context plans; synthetic site, source revocation and audit',
        'tests': ['test_owned_skill_reuse_managed.OwnedSkillReuseManagedTests',
                  'test_owned_skill_knowledge_managed.OwnedSkillKnowledgeManagedTests'],
        'flags': ['AOS_DESKTOP_TESTS', 'AOS_OWNED_SKILL_REUSE_TESTS',
                  'AOS_OWNED_SKILL_KNOWLEDGE_MANAGED_TESTS']},
    'real_learning': {
        'scope': 'Real S1/S2 capture, review/export, tokenizer, CUDA adapter and same-case pair; development only',
        'tests': ['test_owned_episode_managed.OwnedEpisodeManagedTests'],
        'flags': ['AOS_DESKTOP_TESTS', 'AOS_OWNED_ADAPTER_PAIR_TESTS', 'AOS_OWNED_PREPARATION_TESTS']},
}
PROFILES = {'core': ['contracts'], 'ui': ['ui'], 'transport': ['transport'],
            'real': ['real_tasks', 'real_mcp', 'real_takeover', 'real_reuse', 'real_learning'],
            'all': list(CASES)}


def case_environment(case, environment):
    result = {key: value for key, value in environment.items()
              if not (key.startswith('AOS_') and key.endswith('_TESTS'))}
    result['PYTHONPATH'] = os.pathsep.join((str(ROOT), str(ROOT / 'src'), str(ROOT / 'tests')))
    result['PYTHONWARNINGS'] = 'error::ResourceWarning'
    result.update({flag: '1' for flag in CASES[case]['flags']})
    return result


def load_suite(case):
    loader = unittest.TestLoader()
    if case == 'contracts':
        names = [path.stem for path in sorted((ROOT / 'tests').glob('test_*.py'))
                 if path.name != 'test_local_app.py']
        names.append('test_local_app.LocalAppTests')
    else:
        names = CASES[case]['tests']
    return loader.loadTestsFromNames(names)


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active_test = None

    def startTest(self, test):
        self.started = time.monotonic()
        self.active_test = test
        self.outcome = 'unknown'
        super().startTest(test)

    def addSuccess(self, test):
        if self.outcome == 'unknown':
            self.outcome = 'passed'
        super().addSuccess(test)

    def addFailure(self, test, error):
        self.outcome = 'failed'
        super().addFailure(test, error)

    def addError(self, test, error):
        self.outcome = 'error'
        super().addError(test, error)

    def addSkip(self, test, reason):
        if self.active_test is not None:
            if self.outcome not in ('failed', 'error'):
                self.outcome = 'skipped'
        else:
            self.records.append({'test': test.id(), 'status': 'skipped', 'seconds': 0})
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, error):
        self.outcome = 'expected_failure'
        super().addExpectedFailure(test, error)

    def addUnexpectedSuccess(self, test):
        self.outcome = 'unexpected_success'
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, error):
        if error is not None:
            self.outcome = 'failed'
        super().addSubTest(test, subtest, error)

    def stopTest(self, test):
        self.records.append({'test': test.id(), 'status': self.outcome,
                             'seconds': round(time.monotonic() - self.started, 3)})
        self.active_test = None
        super().stopTest(test)


def summarize(records, *, successful):
    counts = {status: sum(row['status'] == status for row in records)
              for status in ('passed', 'skipped', 'failed', 'error', 'expected_failure',
                             'unexpected_success', 'unknown')}
    status = ('failed' if not successful or counts['unknown'] else
              'not_verified' if not counts['passed'] else
              'partial' if counts['skipped'] or counts['expected_failure'] else 'passed')
    return {'status': status, 'counts': counts, 'tests': records}


def private_output(path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_EXCL
    return os.fdopen(os.open(path, flags, 0o600), 'w')


def publish_report(path, report, *, replace=False):
    payload = json.dumps(report, indent=2, allow_nan=False)
    descriptor, temporary = tempfile.mkstemp(prefix='.capability-report-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_real_lock(descriptor):
    metadata = os.fstat(descriptor)
    linked = os.stat(ROOT / 'data/.capability-real.lock', follow_symlinks=False)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (linked.st_dev, linked.st_ino)):
        raise ValueError('Real-model test lock is not a private regular file')
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def real_pool_lock(selected):
    if not any(case.startswith('real_') for case in selected):
        yield None
        return
    descriptor = os.open(ROOT / 'data/.capability-real.lock',
                         os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        validate_real_lock(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def run_worker(case, directory):
    sys.path[:0] = [str(ROOT), str(ROOT / 'src'), str(ROOT / 'tests')]
    started = time.monotonic()
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(load_suite(case))
    report = summarize(result.records, successful=result.wasSuccessful())
    report.update({'case': case, 'scope': CASES[case]['scope'],
                   'seconds': round(time.monotonic() - started, 3),
                   'tests_run': result.testsRun, 'runner_skips': len(result.skipped),
                   'runner_errors': len(result.errors), 'runner_failures': len(result.failures)})
    path = directory / (case + '.json')
    publish_report(path, report)
    return 0 if report['status'] == 'passed' or (
        report['status'] == 'partial' and CASES[case].get('allow_skips')) else 1


def run_pool(selected, real_lock):
    directory = Path(tempfile.mkdtemp(prefix='capability-check-', dir=ROOT / 'data'))
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'cases': [],
              'requested_cases': selected, 'unrun_cases': list(selected),
              'real_site_acceptance': False, 'approval_driver': 'test harness',
              'ordinary_port_lifecycle_tests_excluded': True}
    publish_report(directory / 'report.json', report)
    failed = False
    for case in selected:
        print(f'RUN {case}: {CASES[case]["scope"]}', flush=True)
        lock_arguments = [] if real_lock is None else ['--real-lock-fd', str(real_lock)]
        with private_output(directory / (case + '.log')) as log:
            completed = subprocess.run([sys.executable, '-W', 'error::ResourceWarning', str(Path(__file__).resolve()),
                                        '--case', case, '--worker-output', str(directory), *lock_arguments], cwd=ROOT,
                                       pass_fds=() if real_lock is None else (real_lock,),
                                       env=case_environment(case, os.environ), stdout=log, stderr=subprocess.STDOUT)
        path = directory / (case + '.json')
        evidence = json.loads(path.read_text()) if path.exists() else {
            'case': case, 'scope': CASES[case]['scope'], 'status': 'infrastructure_error'}
        evidence['exit_code'] = completed.returncode
        report['cases'].append(evidence)
        report['unrun_cases'].remove(case)
        failed = failed or completed.returncode != 0
        publish_report(directory / 'report.json', report, replace=True)
        print(f'{evidence["status"].upper()} {case}: {evidence.get("counts", {})}', flush=True)
        if completed.returncode != 0 and case.startswith('real_'):
            print('Stopping real-model pool; inspect logs and owned processes before retrying.', flush=True)
            break
    print(f'Report: {directory.relative_to(ROOT)}/report.json', flush=True)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=PROFILES, default='core')
    parser.add_argument('--case', choices=CASES)
    parser.add_argument('--list', action='store_true', help='Show the selected evidence pool without running it')
    parser.add_argument('--worker-output', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--real-lock-fd', type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    selected = [arguments.case] if arguments.case else PROFILES[arguments.profile]
    if arguments.list:
        print(json.dumps({case: CASES[case] for case in selected}, indent=2))
        return 0
    if arguments.worker_output:
        if not arguments.case:
            parser.error('Worker requires an exact case')
        if arguments.case.startswith('real_'):
            if arguments.real_lock_fd is None:
                parser.error('Real-model worker requires an inherited pool lock')
            validate_real_lock(arguments.real_lock_fd)
        return run_worker(arguments.case, arguments.worker_output)
    try:
        with real_pool_lock(selected) as real_lock:
            return run_pool(selected, real_lock)
    except (OSError, ValueError):
        print('Capability pool could not acquire its private lock or publish evidence; '
              'no automatic retry or lock removal. Inspect existing runs.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
