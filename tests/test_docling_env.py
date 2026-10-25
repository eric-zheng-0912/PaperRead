"""Discovery logic fixtures are not evidence of a second physical computer."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from easyread import docling_env as d, config, settings_api, table_engines


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.a=self.root/'disk-A/anaconda/envs/research/python.exe'
        self.b=self.root/'disk-B/miniforge/envs/tables/python.exe'
        for p in (self.a,self.b):p.parent.mkdir(parents=True);p.write_bytes(b'fixture')
        self.candidates=[{'python':str(self.a),'origin':'conda-registration'},{'python':str(self.b),'origin':'windows-registration'}]
        self.mp=patch.object(d,'models',return_value=(self.root,[]));self.mp.start();self.addCleanup(self.mp.stop)
        self.rp=patch.object(d,'registered',return_value=self.candidates);self.rp.start();self.addCleanup(self.rp.stop)
        self.probe=patch.object(d,'probe',return_value={'status':'basic_import','docling':'2.126.0','inference':False}).start()
        self.addCleanup(patch.stopall);d._cache.clear()

    def test_registered_order_stable_and_cross_roots(self):
        r=d.discover({});self.assertEqual(r['selected']['python'],str(self.a))
        self.assertEqual(len(r['candidates']),2);self.assertEqual(r['status'],'basic_import')
        self.assertFalse(r['selected']['inference'])

    def test_explicit_priority(self):
        self.assertEqual(d.discover({'docling_python':str(self.b)})['selected']['python'],str(self.b))

    def test_invalid_override_visible_and_repairable(self):
        r=d.discover({'docling_python':str(self.root/'missing/python.exe')})
        self.assertEqual(r['candidates'][0]['status'],'unavailable')
        self.assertIn('redetect',r['candidates'][0]['reason']);self.assertEqual(r['selected']['python'],str(self.a))

    def test_missing_docling_fallback(self):
        self.probe.return_value={'status':'unavailable','reason':'not installed'}
        self.assertIsNone(d.discover({})['selected'])
        with patch.object(d,'discover',return_value=d.discover({})):
            with self.assertRaises(table_engines.WorkerUnavailable):table_engines._environment({})

    def test_models_missing_not_certified(self):
        with patch.object(d,'models',side_effect=ValueError('missing weights')):
            r=d.discover({})
        self.assertIsNone(r['selected']);self.assertEqual(r['candidates'][0]['status'],'incomplete')

    def test_dependency_missing_not_certified(self):
        self.probe.return_value={'status':'incomplete','reason':'torch missing','docling':'2.1.0'}
        self.assertIsNone(d.discover({})['selected'])

    def test_check_cache_no_duplicate_process(self):
        d.discover({});d.discover({});self.assertEqual(self.probe.call_count,2)
        d.discover({},refresh=True);self.assertEqual(self.probe.call_count,4)

    def test_user_config_persists_only_isolated_home(self):
        defaults=copy.deepcopy(config.DEFAULTS)
        with patch.object(config,'HOME',self.root),patch.object(config,'CONFIG_PATH',self.root/'config.json'),patch.object(config,'_SOURCE_MODE',True),patch.object(config,'INSTALL_ROOT',None):
            config.save({'docling_python':str(self.b)})
            self.assertEqual(config.load()['docling_python'],str(self.b))
        self.assertEqual(config.DEFAULTS,defaults)
        with patch.object(config,'HOME',self.root/'user2'),patch.object(config,'CONFIG_PATH',self.root/'user2/config.json'),patch.object(config,'_SOURCE_MODE',True),patch.object(config,'INSTALL_ROOT',None):
            self.assertEqual(config.load()['docling_python'],'')

    def test_api_override_does_not_save(self):
        with patch.object(config,'load',return_value={}),patch.object(config,'save') as save:
            r=settings_api.docling_status({'python':str(self.b)})
        save.assert_not_called();self.assertEqual(r['selected']['python'],str(self.b))


class ProcessTest(unittest.TestCase):
    def test_timeout_bounded_and_fixed_entry(self):
        with patch.object(subprocess,'run',side_effect=subprocess.TimeoutExpired('worker',1)) as run:
            r=d.probe(Path('python.exe'),Path('hub'),1)
        self.assertEqual(r['status'],'unavailable')
        args=run.call_args.args[0];self.assertEqual(args[1:3],['-I','-B'])
        self.assertTrue(args[-1].endswith('docling_worker.py'))
        self.assertEqual(run.call_args.kwargs['env']['HF_HUB_OFFLINE'],'1')

    def test_abnormal_exit_falls_back(self):
        with patch.object(subprocess,'run',return_value=subprocess.CompletedProcess([],3,'','native failure')):
            r=d.probe(Path('python.exe'),Path('hub'))
        self.assertEqual(r['status'],'unavailable');self.assertIn('native failure',r['reason'])

    def test_conda_registry_without_path_or_env_name_assumption(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);env=root/'other-drive/unusual-name';env.mkdir(parents=True)
            (env/'python.exe').write_bytes(b'fixture');(root/'.conda').mkdir()
            (root/'.conda/environments.txt').write_text(str(env),encoding='utf8')
            with patch.object(d.shutil,'which',return_value=None):r=d.registered(root)
            self.assertIn(str((env/'python.exe').resolve()),[x['python'] for x in r])


if __name__=='__main__':unittest.main()
