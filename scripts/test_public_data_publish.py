"""Portable publisher CLI regression tests; HTTP and Git are always mocked."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location('tested_public_publisher',ROOT/'public_data_publish.py')
publisher=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publisher)
CANARY='DO_NOT_EMIT_TEST_SENTINEL'


class DiagnosticTests(unittest.TestCase):
    def cli(self,stderr=None,code=1,changed=True,actions='true'):
        calls=[]
        stream=io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'config').mkdir()
            policy=root/'config'/'collection-policy.json'
            policy.write_text(json.dumps({'sources':[{'mode':'discovery','cadenceMinutes':360}]}))
            before=hashlib.sha256(policy.read_bytes()).hexdigest()
            def process(args,**kwargs):
                calls.append(tuple(args))
                self.assertTrue(kwargs['capture_output'])
                self.assertTrue(kwargs['check'])
                self.assertTrue(kwargs['text'])
                if args[:2]==('git','status'):
                    return subprocess.CompletedProcess(args,0,' M docs/data/snapshot.json\n' if changed else '','')
                if args[:2]==('git','push'):
                    self.assertEqual(args,('git','push','origin','HEAD:main'))
                    self.assertIn('AUTHORIZATION: basic ',kwargs['env']['GIT_CONFIG_VALUE_0'])
                    if stderr is not None:
                        raise subprocess.CalledProcessError(code,args,output=CANARY+' stdout',stderr=stderr)
                return subprocess.CompletedProcess(args,0,'c'*40+'\n','')
            env={'GITHUB_ACTIONS':actions,'GITHUB_REPOSITORY':'brand-on-fire/peace-corps-security-data','GITHUB_REF_NAME':'main','GITHUB_TOKEN':CANARY}
            with mock.patch.dict(os.environ,env,clear=True), mock.patch('pathlib.Path.cwd',return_value=root), mock.patch('subprocess.run',side_effect=process), mock.patch('urllib.request.urlopen',return_value=io.BytesIO(b'{"private":false,"visibility":"public","size":100}')) as http, mock.patch.object(sys,'argv',['public_data_publish.py','--publish']),contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
                try:
                    runpy.run_path(str(ROOT/'public_data_publish.py'),run_name='__main__')
                    exit_code=0
                except SystemExit as error:
                    exit_code=error.code
            self.assertEqual(http.call_count,1 if actions=='true' else 0)
            self.assertEqual(hashlib.sha256(policy.read_bytes()).hexdigest(),before)
        self.assertFalse(any('fetch' in c or 'rebase' in c or '--force' in c for c in calls))
        return stream.getvalue(),exit_code,calls

    def assert_failure(self,stderr,kind,code=1):
        output,exit_code,calls=self.cli(stderr,code)
        self.assertEqual(exit_code,1)
        self.assertEqual(output,f'::error title=Public data publication::git-push-failed category={kind} exit={code}\nPublic-data operation refused: RuntimeError: git-push-failed category={kind} exit={code}\n')
        self.assertEqual(len(output.splitlines()),2)
        self.assertEqual(len([line for line in output.splitlines() if line.startswith('::error')]),1)
        self.assertEqual(sum(c[:2]==('git','push') for c in calls),1)
        self.assertNotIn(CANARY,output)
        self.assertNotIn('HEAD:main',output)
        self.assertNotIn('AUTHORIZATION',output)
        return output

    def test_non_fast_forward(self):
        self.assert_failure('To https://'+CANARY+'@github.com/org/repo\n ! [rejected] HEAD -> main (non-fast-forward)\nerror: failed to push some refs','non-fast-forward')

    def test_fetch_first(self):
        self.assert_failure(' ! [rejected] HEAD -> main (fetch first)\n'+CANARY,'non-fast-forward')

    def test_permission_denied(self):
        self.assert_failure('remote: Permission to org/repo.git denied to '+CANARY+'.\nfatal: unable to access private URL','permission',128)

    def test_write_access_denied(self):
        self.assert_failure('remote: Write access to repository not granted.\n'+CANARY,'permission',128)

    def test_authentication_denied(self):
        self.assert_failure("fatal: Authentication failed for 'https://"+CANARY+"@github.com/org/repo'",'permission',128)

    def test_ssh_denied(self):
        self.assert_failure('git@github.com: Permission denied (publickey).\n'+CANARY,'permission',128)

    def test_unknown_network_failure(self):
        self.assert_failure('fatal: unable to access https://'+CANARY+'@github.com/org/repo: TLS error','unknown',128)

    def test_unknown_http_403(self):
        self.assert_failure('fatal: unable to access '+CANARY+': The requested URL returned error: 403','unknown',128)

    def test_branch_protection_not_misclassified(self):
        self.assert_failure('remote: error: GH013: Repository rule violations found\n ! [remote rejected] HEAD -> main (push declined due to repository rule violations)\n'+CANARY,'unknown')

    def test_conflicting_signatures_fail_closed(self):
        self.assert_failure(' ! [rejected] HEAD -> main (non-fast-forward)\nremote: Permission to org/repo denied to '+CANARY+'.','unknown')

    def test_non_string_and_empty(self):
        for text in [None,b' ! [rejected] HEAD -> main (fetch first)','']:
            with self.subTest(kind=type(text).__name__):
                self.assertEqual(publisher.classify_push_failure(text),'unknown')

    def test_unrelated_quoted_words_not_matched(self):
        for text in ['hint: message mentions non-fast-forward',CANARY+' Permission denied', 'remote: notice: Authentication failed for demonstration']:
            with self.subTest(text=text):
                self.assertEqual(publisher.classify_push_failure(text),'unknown')

    def test_annotation_rejects_stderr_injection(self):
        self.assert_failure(' ! [rejected] HEAD -> main (fetch first)\n::error title='+CANARY+'::injected\r\n'+CANARY+'%0A%0D%3A', 'non-fast-forward',128)

    def test_annotation_numeric_signal_exit(self):
        self.assert_failure(CANARY+'\nnetwork failure','unknown',-9)

    def test_outside_actions_unchanged(self):
        for actions in ['false','','TRUE']:
            with self.subTest(actions=actions):
                output,exit_code,calls=self.cli(CANARY,actions=actions)
                self.assertEqual(exit_code,1)
                self.assertEqual(output,'Public-data operation refused: ValueError: Publishing only runs inside its audited GitHub Actions workflow\n')
                self.assertEqual(calls,[])
                self.assertNotIn('::error',output)
                self.assertNotIn(CANARY,output)

    def test_success_unchanged(self):
        output,exit_code,calls=self.cli()
        self.assertEqual(exit_code,0)
        self.assertEqual(json.loads(output),{'published':True,'commit':'c'*40})
        self.assertEqual(sum(c[:2]==('git','push') for c in calls),1)
        self.assertNotIn(CANARY,output)
        self.assertNotIn('::error',output)

    def test_no_changes_does_not_push(self):
        output,exit_code,calls=self.cli(changed=False)
        self.assertEqual(exit_code,0)
        self.assertEqual(json.loads(output),{'published':False,'reason':'No changed data'})
        self.assertFalse(any(c[:2]==('git','push') for c in calls))
        self.assertNotIn('::error',output)



if __name__=='__main__':
    unittest.main(verbosity=2)
