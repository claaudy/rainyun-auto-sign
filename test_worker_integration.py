"""Offline Docker integration; ALL imagery/TDC are synthetic fixtures, not API proof.
Run as root: RUN_WORKER_INTEGRATION=1 python3 -m unittest -v test_worker_integration
"""
import importlib.util
import os
from pathlib import Path
import unittest
import rainyun_http as m


class TestRunnersExist(unittest.TestCase):
    def test_isolated_tdc_runner_available(self):
        self.assertTrue((m.ROOT / 'tdc_runner.js').exists(), 'isolated TDC runner missing')

    def test_raw_matcher_adapter_available(self):
        self.assertTrue((m.ROOT / 'tenvision_raw.py').exists(), 'raw matcher adapter missing')
        self.assertTrue((m.ROOT / 'LICENSE-TenVision').exists(), 'MIT attribution missing')
        self.assertIn('ins_elem_cfg', (m.ROOT / 'tenvision_raw.py').read_text())


@unittest.skipUnless(os.getenv('RUN_WORKER_INTEGRATION') == '1' and os.geteuid() == 0,
                     'root + explicit offline Docker integration opt-in required')
class TestDocker(unittest.TestCase):
    def setUp(self):
        self.worker = m.Worker(os.getenv('WORKER_IMAGE', m.DEFAULT_WORKER_IMAGE))

    def test_tdc_fixture_decodes_collect_and_js_string_length(self):
        self.assertTrue((m.ROOT / 'tdc_runner.js').exists(), 'isolated TDC runner missing')
        with m.PrivateWorkspace() as directory:
            tdc = m.private_file(directory / 'tdc.js', b'window.TDC={getData:()=>"A%F0%9F%98%80B",getInfo:()=>({info:"FIXTURE_EKS",tokenid:"FIXTURE_TKID"})};', True)
            runner = m.private_file(directory / 'runner.js', (m.ROOT / 'tdc_runner.js').read_bytes(), True)
            proof = self.worker.run('node', [(tdc, '/tdc.js'), (runner, '/runner.js')], ['/runner.js'], directory)
        self.assertEqual(proof, {'collect': 'A\U0001f600B', 'tlg': 4, 'eks': 'FIXTURE_EKS', 'tkid': 'FIXTURE_TKID'})

    def test_runtime_is_nonroot_capless_networkless_and_browser_free(self):
        script = b'''import json,os,pathlib,socket,importlib.util
status=pathlib.Path('/proc/self/status').read_text()
assert os.getuid()==65534
assert 'NoNewPrivs:\\t1' in status
assert 'CapEff:\\t0000000000000000' in status
assert importlib.util.find_spec('playwright') is None
assert not any(pathlib.Path(p).exists() for p in ['/usr/bin/chromium','/usr/bin/chromium-browser','/usr/bin/google-chrome','/var/run/docker.sock'])
try: pathlib.Path('/forbidden-write').write_text('fixture')
except OSError: pass
else: raise AssertionError('rootfs writable')
s=socket.socket();s.settimeout(1)
try: s.connect(('1.1.1.1',443))
except OSError: pass
else: raise AssertionError('network available')
finally: s.close()
import cv2,numpy
print(json.dumps({'uid':os.getuid(),'browser':False,'network':False,'cv2':cv2.__version__,'numpy':numpy.__version__}))
'''
        with m.PrivateWorkspace() as directory:
            runner = m.private_file(directory / 'test.py', script, True)
            result = self.worker.run('python3', [(runner, '/test.py')], ['/test.py'], directory)
        self.assertEqual(result, {'uid':65534,'browser':False,'network':False,'cv2':'4.12.0','numpy':'2.2.6'})

    def test_rys_matches_actual_javascript_reference(self):
        import json,random
        rng = random.Random(42)
        values = [1700000000000,1700000000100,1700000000010,1700000000001] + [rng.randrange(1500000000000,1800000000000) for _ in range(100)]
        script = 'const ms=' + json.dumps(values) + ';process.stdout.write(JSON.stringify({values:ms.map(x=>String(0+x/1000))}))'
        with m.PrivateWorkspace() as directory:
            reference = self.worker.run('node', [], ['-e', script], directory)
        self.assertEqual(reference['values'], [m.rys_header(v) for v in values])

    def test_tdc_infinite_loop_is_bounded(self):
        with m.PrivateWorkspace() as directory:
            tdc = m.private_file(directory / 'tdc.js', b'while(true){}', True)
            runner = m.private_file(directory / 'runner.js', (m.ROOT / 'tdc_runner.js').read_bytes(), True)
            with self.assertRaises(m.Failure):
                self.worker.run('node', [(tdc, '/tdc.js'), (runner, '/runner.js')], ['/runner.js'], directory)

    def test_synthetic_raw_matcher_uses_metadata_and_native_coordinates(self):
        self.assertTrue((m.ROOT / 'tenvision_raw.py').exists(), 'raw matcher adapter missing')
        script = b'''import sys,json,tempfile,pathlib
sys.path.insert(0,"/code")
import cv2,numpy as np
from tenvision_raw import solve, instruction_masks, normalize_mask, crop_foreground
sprite=np.full((65,190,3),255,np.uint8)
# Three distinct fixture glyphs at nonzero sprite offsets.
for i,c in enumerate(["L","T","O"]): cv2.putText(sprite,c,(12+i*60,48),cv2.FONT_HERSHEY_SIMPLEX,1.25,(0,0,0),4)
background=np.full((220,410,3),255,np.uint8)
for c,p in [("L",(40,105)),("T",(170,165)),("O",(300,95))]: cv2.putText(background,c,p,cv2.FONT_HERSHEY_SIMPLEX,2.0,(0,0,0),6)
cfg={"ins_elem_cfg":[{"sprite_pos":[5+i*60,5],"size_2d":[55,55]} for i in range(3)],"bg_elem_cfg":{"size_2d":[410,220]}}
with tempfile.TemporaryDirectory() as d:
 s=pathlib.Path(d)/"sprite.png";b=pathlib.Path(d)/"bg.png"
 cv2.imwrite(str(s),sprite);cv2.imwrite(str(b),background)
 result=solve(s,b,cfg)
 assert len(result["points"])==3
 assert 35 < result["points"][0][0] < 95, result
 assert 160 < result["points"][1][0] < 220, result
 assert 290 < result["points"][2][0] < 360, result
 try: solve(s,b,{"ins_elem_cfg":[],"bg_elem_cfg":{"size_2d":[1,1]}})
 except ValueError: pass
 else: raise AssertionError("invalid metadata accepted")
print(json.dumps({"fixture":"synthetic_only","points_native":True,"count":len(result["points"])}))
'''
        with m.PrivateWorkspace() as directory:
            matcher = m.private_file(directory / 'matcher.py', (m.ROOT / 'tenvision_raw.py').read_bytes(), True)
            runner = m.private_file(directory / 'test.py', script, True)
            result = self.worker.run('python3', [(matcher, '/code/tenvision_raw.py'), (runner, '/test.py')], ['/test.py'], directory)
        self.assertEqual(result, {'fixture': 'synthetic_only', 'points_native': True, 'count': 3})


if __name__ == '__main__':
    unittest.main()
