"""DOCX fidelity, strict ordering, and real Qt harness against a fake Ollama."""
import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('prompt_campaign', ROOT / 'tools/prompt_campaign.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_document_fidelity_and_order():
    user = ROOT / 'tools/campaign_inputs/User prompt.docx'
    adversarial = ROOT / 'tools/campaign_inputs/Adversarial prompt.docx'
    normal = runner.read_prompts(user)
    assert normal[0] == 'N001\tWhat is in the open network, including its size, driving side, roads, junctions, boundary ends and bus stops?'
    try:
        runner.manifest(user, adversarial, 'N', 200)
    except ValueError:
        pass
    else:
        assert False, 'Missing N001–N200 must fail before submission'
    plan = runner.manifest(user, adversarial, 'A', 100)
    assert len(plan) == 900
    assert [(plan[i]['model'], plan[i]['id']) for i in (0,199,200,299,300,899)] == [
        ('qwen3.5:4b','N001'), ('qwen3.5:4b','N200'), ('qwen3.5:4b','A001'),
        ('qwen3.5:4b','A100'), ('llama3.2:3b','N001'), ('gemma4:e4b','A100')]
    for changed in (normal[:-1], normal + normal[:1], normal[1:] + normal[:1]):
        try:
            runner.validate(changed, 'N', 200)
        except ValueError:
            pass
        else:
            assert False


def test_real_qt_campaign_without_real_prompt_submissions():
    from http.server import HTTPServer
    import threading
    fake_spec = importlib.util.spec_from_file_location('fake_research', ROOT / 'tests/test_research_log.py')
    fake = importlib.util.module_from_spec(fake_spec)
    fake_spec.loader.exec_module(fake)
    server = HTTPServer(('127.0.0.1', 0), fake._Ollama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    previous = os.environ.get('OLLAMA_HOST')
    os.environ['OLLAMA_HOST'] = f'http://127.0.0.1:{server.server_port}'
    from traffic_simulator.main import MainWindow
    from traffic_simulator.theme import apply_theme
    apply_theme(fake.APP)
    window = MainWindow()
    window._ask_to_save = False
    folder = Path(tempfile.mkdtemp(prefix='acms-harness-'))
    window.results_folder = folder / 'research'
    window.show()
    plan = [dict(model='small:3b', group='User' if i == 1 else 'Adversarial', id=f'N{i:03d}', prompt=f'N{i:03d}\t' +
                 ('Build me a 3x2 grid, run it 5 minutes' if i == 1 else 'Build me a 2x2 grid, run it 5 minutes')) for i in (1,2)]
    execute = window.ai_mode._execute
    executed = []
    def raise_on_second_apply(name, args):
        if name == 'build_scenario':
            executed.append(name)
            if len(executed) == 2:
                raise KeyError('all')
        return execute(name, args)
    window.ai_mode._execute = raise_on_second_apply
    try:
        campaign = runner.Campaign(window, plan, folder / 'campaign', timeout=30)
        deadline = time.monotonic() + 40
        while campaign.phase != 'complete' and campaign.failure is None and time.monotonic() < deadline:
            fake.APP.processEvents()
            time.sleep(.01)
        assert campaign.failure is None, campaign.failure
        assert campaign.phase == 'complete'
        rows = [json.loads(line) for line in campaign.path.read_text(encoding='utf-8').splitlines()]
        assert [r['id'] for r in rows if r['event'] == 'submitted'] == ['N001','N002']
        assert [r['prompt'] for r in rows if r['event'] == 'completed'] == [p['prompt'] for p in plan]
        assert [r['tool'] for r in rows if r['event'] == 'applied'] == ['build_scenario', 'build_scenario']
        assert len([r for r in rows if r['event'] == 'apply_intent']) == 2
        assert not window.ai_mode._pending
        applied = [r for r in rows if r['event'] == 'applied']
        assert "KeyError: 'all'" in applied[1]['result']['error']
        assert [r['event'] for r in rows].index('applied') < [r['event'] for r in rows].index('completed')
        assert window.activity.on
        try:
            runner.Campaign(window, plan, folder / 'campaign')
        except ValueError:
            pass
        else:
            assert False, 'Must refuse duplicate campaign launch'
    finally:
        window.watch_action.setChecked(False)
        window.close()
        server.shutdown()
        if previous is None:
            os.environ.pop('OLLAMA_HOST', None)
        else:
            os.environ['OLLAMA_HOST'] = previous


def test_multiple_cards_and_reentrant_apply():
    from types import SimpleNamespace as NS
    campaign = runner.Campaign.__new__(runner.Campaign)
    campaign.plan = [dict(id='A001', model='mock', prompt='A001\tTest')]
    campaign.index = 0
    campaign.active = campaign.plan[0]
    campaign.phase = 'answering'
    campaign.timeout = 30
    campaign.began = time.monotonic()
    campaign._applying = None
    events, clicks = [], []
    campaign.record = lambda event, **fields: events.append((event, fields))
    campaign.window = NS(activity=NS(log=lambda *args: None))
    panel = NS(_step=dict(prompt=campaign.active['prompt']),
               proposal=NS(isHidden=lambda: False))
    campaign.panel = panel

    def click():
        clicks.append(panel._pending)
        campaign.advance()  # headless execution can pump Qt's event loop
        panel._pending.result = {'ok': True}

    panel.apply_button = NS(isEnabled=lambda: True, click=click)
    for tool in ('first_edit', 'second_edit'):
        panel._pending = NS(call=dict(name=tool, arguments={}), result=None)
        campaign.advance()
        campaign.advance()  # same card still present: must never click again
    assert len(clicks) == 2
    assert [fields['tool'] for event, fields in events if event == 'applied'] == ['first_edit', 'second_edit']
    panel._pending = NS(call=dict(name='wrong_step', arguments={}), result=None)
    panel._step['prompt'] = 'A999\tOther prompt'
    try:
        campaign.advance()
    except RuntimeError:
        pass
    else:
        assert False, 'Must not apply a card from another prompt'
    assert len(clicks) == 2


def test_live_update_retains_pending_request_and_ledger():
    from types import SimpleNamespace as NS
    spec = importlib.util.spec_from_file_location('campaign_apply_test', ROOT / 'extensions/campaign_apply.py')
    extension = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(extension)
    campaign = runner.Campaign.__new__(runner.Campaign)
    pending, ledger = object(), object()
    events = []
    campaign.phase, campaign.active, campaign.failure = 'answering', {'id': 'N011'}, None
    campaign.panel = NS(_pending=pending)
    campaign.path = ledger
    campaign.index = 10
    campaign.timer = NS(isActive=lambda: True)
    campaign.record = lambda event, **fields: events.append((event, fields))
    extension.activate(campaign, runner.Campaign.advance)
    assert campaign.panel._pending is pending and campaign.path is ledger
    assert campaign.index == 10 and campaign.active['id'] == 'N011'
    assert campaign.advance.__func__ is runner.Campaign.advance
    assert events == [('driver_updated', {'automatic_apply': 'both', 'id': 'N011'})]
    campaign.failure = 'Timed out'
    try:
        extension.activate(campaign, runner.Campaign.advance)
    except ValueError:
        pass
    else:
        assert False, 'Must not resume a halted campaign blindly'
    request = NS(result=None, call={'name': 'build_scenario'})
    returned, started = [], []
    campaign.failure = 'Deadline exceeded; no retry. Inspect ledger and Research Log.'
    campaign._applying = request
    campaign.window = NS(_testing=False)
    campaign.panel = NS(_pending=request, _step={'prompt': 'N011\tTest'},
                        proposal=NS(isHidden=lambda: True),
                        _settle_pending=lambda result: returned.append(result))
    campaign.active['prompt'] = 'N011\tTest'
    campaign.timer = NS(isActive=lambda: False, start=lambda ms: started.append(ms))
    extension.activate(campaign, runner.Campaign.advance)
    assert len(returned) == 1 and 'not repeated' in returned[0]['error']
    assert started == [100] and campaign.failure is None and campaign.index == 10
    assert campaign.active['id'] == 'N011'


if __name__ == '__main__':
    for name, function in list(globals().items()):
        if name.startswith('test_'):
            function()
    print('ok')
