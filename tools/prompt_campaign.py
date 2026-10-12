"""Run numbered DOCX prompts through the real ACMS Qt panel, one at a time.

No external desktop helper or new dependencies. No automatic retries,
or ambiguous crash recovery. See docs/architecture/prompt_campaign.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
MODELS = ('qwen3.5:4b', 'llama3.2:3b', 'gemma4:e4b')
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def read_prompts(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
    prompts = []
    for paragraph in root.iter(W + 'p'):
        text = ''.join((node.text or '') if node.tag == W + 't' else
                       '\t' if node.tag == W + 'tab' else
                       '\n' if node.tag in (W + 'br', W + 'cr') else ''
                       for run in paragraph.findall(W + 'r') for node in run.iter())
        if re.match(r'^[NA]\d{3}(?:\s|$)', text):
            prompts.append(text)
    return prompts


def validate(prompts: list[str], prefix: str, count: int) -> None:
    ids = [re.match(r'^([NA]\d{3})', text).group(1) for text in prompts]
    expected = [f'{prefix}{number:03d}' for number in range(1, count + 1)]
    if ids != expected:
        raise ValueError(f'Expected {prefix}001–{prefix}{count:03d}; found '
                         f'{len(ids)} prompts ({ids[:1]} … {ids[-1:]}). Nothing submitted.')


def manifest(user: Path, adversarial: Path, prefix='A', count=100) -> list[dict]:
    groups = [('User', user, 'N', 200), ('Adversarial', adversarial, prefix, count)]
    parsed = []
    for group, path, letter, size in groups:
        prompts = read_prompts(path)
        validate(prompts, letter, size)
        parsed.append((group, path, prompts))
    return [dict(model=model, group=group, id=text[:4], prompt=text,
                 source=str(path.resolve()), sha256=hashlib.sha256(text.encode()).hexdigest())
            for model in MODELS for group, path, prompts in parsed for text in prompts]


class Campaign:
    """Qt event-loop driver. Applies validated cards for both approved prompt sets."""

    def __init__(self, window, plan, output: Path, timeout=3600):
        from PySide6.QtCore import QTimer
        self.window, self.panel, self.plan = window, window.ai_mode, plan
        self.output, self.timeout = output, timeout
        output.mkdir(parents=True, exist_ok=True)
        self.path = output / 'ledger.jsonl'
        if self.path.exists():
            raise ValueError('Existing ledger: refusing an automatic restart or duplicate submission.')
        (output / 'manifest.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
        self.index, self.phase, self.active, self.began = 0, 'select', None, time.monotonic()
        self.failure = None
        self._applying = None
        self.panel._relay.done.connect(self.finished)
        self.timer = QTimer(window)
        self.timer.timeout.connect(self.tick)
        window.tabs.setCurrentWidget(window.extensions)
        window.watch_action.trigger()
        if not window.activity.on:
            raise RuntimeError('Click logging did not start')
        window.extensions.find('Research Log').check.click()
        window.tabs.setCurrentWidget(self.panel)
        self.panel.show_system_2()
        self.timer.start(100)

    def record(self, event, **fields):
        with self.path.open('a', encoding='utf-8') as file:
            file.write(json.dumps(dict(event=event, at=time.time(), index=self.index, **fields),
                                  ensure_ascii=False) + '\n')
            file.flush()
            os.fsync(file.fileno())

    def stop(self, error):
        self.failure = error
        self.timer.stop()
        self.record('halted', error=error)
        self.window.statusBar().showMessage('Campaign halted: ' + error)

    def finished(self, session, job, result, error):
        if session != self.panel._session:
            return
        if error:
            self.stop(error)
        elif job == 'answer' and self.active is not None:
            if self.panel._step is not None:
                self.stop('Research Log completion not observed')
                return
            extension = self.window.extensions.find('Research Log')
            if not extension.on or extension.error:
                self.stop('Research Log failed')
                return
            log = extension.module
            folder = log._session['folder']
            steps = log.steps(folder)
            if not steps or steps[-1].get('prompt') != self.active['prompt']:
                self.stop('Recorded prompt differs from submitted prompt')
                return
            self.record('completed', **self.active, research_folder=str(folder))
            self.index += 1
            self.active = None
            self.phase = 'select'
            self.began = time.monotonic()

    def tick(self):
        try:
            self.advance()
        except Exception as error:
            self.stop(repr(error))

    def advance(self):
        if self.index == len(self.plan):
            self.timer.stop()
            self.window.extensions.find('Research Log').module.end_session()
            self.record('campaign_completed', submissions=self.index)
            self.phase = 'complete'
            self.window.statusBar().showMessage(f'Campaign complete: {self.index} submissions')
            return
        if time.monotonic() - self.began > self.timeout:
            self.stop('Deadline exceeded; no retry. Inspect ledger and Research Log.')
            return
        item = self.plan[self.index]
        if self.phase == 'answering' and self.active is not None:
            request = self.panel._pending
            if request is not None and request is not self._applying:
                if self.panel.proposal.isHidden() or not self.panel.apply_button.isEnabled():
                    return
                if self.panel._step is None or self.panel._step.get('prompt') != self.active['prompt']:
                    raise RuntimeError('Approval card does not belong to the active prompt')
                self._applying = request
                self.record('apply_intent', id=item['id'], model=item['model'],
                            tool=request.call['name'], arguments=request.call['arguments'])
                self.window.activity.log('automation', 'Apply', request.call['name'])
                self.panel.apply_button.click()
                if request.result is None and not getattr(self.window, '_testing', False):
                    raise RuntimeError('Apply callback returned without a tool result; inspect the process traceback')
                self.record('applied', id=item['id'], model=item['model'],
                            tool=request.call['name'], result=request.result)
            return
        if self.phase == 'select':
            if self.panel._running or self.panel._pending is not None:
                return
            if self.panel._model != item['model']:
                local = self.panel.provider.findData('local')
                model = self.panel.local_models.findText(item['model'])
                if local < 0 or model < 0:
                    raise ValueError('Required Ollama model unavailable: ' + item['model'])
                self.panel.provider.setCurrentIndex(local)
                self.panel.provider.activated.emit(local)
                self.panel.local_models.setCurrentIndex(model)
                self.panel.local_models.activated.emit(model)
                self.phase = 'warming'
                self.panel.start_button.click()
                return
            self.phase = 'warming'
        if self.phase == 'warming' and self.panel.prompt.isEnabled() and not self.panel._running:
            self.active = item
            self._applying = None
            self.record('submission_intent', **item)
            self.phase = 'answering'
            self.panel.prompt.setText(item['prompt'])
            self.panel.send_button.click()
            if not self.panel._running:
                raise RuntimeError('Submission did not start; refusing to retry')
            self.record('submitted', **item)
            self.began = time.monotonic()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user', type=Path, default=ROOT / 'tools/campaign_inputs/User prompt.docx')
    parser.add_argument('--adversarial', type=Path, default=ROOT / 'tools/campaign_inputs/Adversarial prompt.docx')
    parser.add_argument('--adversarial-prefix', choices=['N', 'A'], default='A')
    parser.add_argument('--adversarial-count', type=int, default=100)
    parser.add_argument('--output', type=Path, default=ROOT / 'reports/prompt_campaign')
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args()
    plan = manifest(args.user, args.adversarial, args.adversarial_prefix, args.adversarial_count)
    if args.preflight:
        print(f'Validated {len(plan)} submissions. Nothing submitted.')
        return 0
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from traffic_simulator.main import MainWindow
    from traffic_simulator.theme import apply_theme
    # Match run.py's graphics initialization before any Qt application exists.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL)
    app = QApplication(sys.argv)
    app.setApplicationName('ACMS')
    apply_theme(app)
    window = MainWindow()
    window.show()
    campaign = Campaign(window, plan, args.output)
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
