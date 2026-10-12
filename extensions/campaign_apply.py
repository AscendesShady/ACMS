"""Opt-in live update of an already-running prompt campaign.

Reload Extensions, then tick Campaign Apply to update the harness in memory.
No submission, network mutation or action occurs while this extension is off.
"""
import gc
import importlib.util
import time
from pathlib import Path
from types import MethodType

NAME = 'Campaign Apply'
DESCRIPTION = 'Update the running prompt campaign'
ORDER = 6
_updated = None


def protect_apply(panel):
    """Guard the old process's callback without reloading its Qt class."""
    original = panel._decide
    def guarded(apply):
        request = panel._pending
        try:
            return original(apply)
        except Exception as exc:
            if request is None or panel._pending is not request:
                raise
            result = {'error': f'Apply failed: {type(exc).__name__}: {exc}. '
                              'Inspect the project for partial changes before retrying.'}
            if request.record is not None:
                request.record.update(decision='applied' if apply else 'skipped',
                                      decided_at=time.time(), result=result)
            panel.log(request.call['name'], request.call['arguments'], result['error'])
            panel._say('Error', result['error'])
            panel._settle_pending(result)
    panel._decide = guarded


def activate(campaign, advance):
    """Replace only the driver method; retain worker, pending card and ledger."""
    if campaign.phase != 'answering' or campaign.active is None:
        raise ValueError('Live update requires an active campaign submission')
    recovering = campaign.failure == 'Deadline exceeded; no retry. Inspect ledger and Research Log.'
    if campaign.failure is not None and not recovering:
        raise ValueError('Campaign is halted; reconcile the ledger before continuing')
    if recovering:
        panel = campaign.panel
        request = panel._pending
        if (request is None or request is not campaign._applying or request.result is not None
                or panel._step is None or panel._step.get('prompt') != campaign.active['prompt']
                or not panel.proposal.isHidden() or getattr(campaign.window, '_testing', False)):
            raise ValueError('Timed-out state is ambiguous; no prompt or tool call was repeated')
        # The synchronous Apply callback returned without settling the hidden
        # card. Return an explicit error to the existing worker, never execute it
        # again or invent a successful result.
        result = {'error': 'The previous Apply callback failed without returning a result. '
                          'Inspect the process traceback and project for partial changes. '
                          'The original operation was not repeated.'}
        campaign.record('recovered_failed_apply', id=campaign.active['id'],
                        tool=request.call['name'], result=result)
        panel._settle_pending(result)
        campaign.failure = None
        campaign.began = time.monotonic()
        campaign.timer.start(100)
    elif not campaign.timer.isActive():
        raise ValueError('Campaign timer is inactive; reconcile before continuing')
    if hasattr(campaign.panel, '_decide'):
        protect_apply(campaign.panel)
    campaign._applying = None
    campaign.advance = MethodType(advance, campaign)
    campaign.record('driver_updated', automatic_apply='both', id=campaign.active['id'])


def draw(network, t):
    global _updated
    if _updated is not None:
        return []
    # The original CLI did not retain a public window.campaign reference. Locate
    # exactly that same-process driver, never another application or a new one.
    candidates = [obj for obj in gc.get_objects()
                  if type(obj).__name__ == 'Campaign' and type(obj).__module__ == '__main__']
    if len(candidates) != 1:
        raise ValueError(f'Expected one running CLI campaign; found {len(candidates)}')
    campaign = candidates[0]
    path = Path(__file__).resolve().parents[1] / 'tools/prompt_campaign.py'
    spec = importlib.util.spec_from_file_location('acms_campaign_live_update', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    activate(campaign, module.Campaign.advance)
    _updated = campaign
    return []
