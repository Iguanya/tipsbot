"""Payment provider registry. Add a provider = add a module with CODE, LABEL, available(), start()."""
import logging

import config as cfg
from . import mpesa_daraja, mpesa_manual, stars

log = logging.getLogger(__name__)
PROVIDERS = {m.CODE: m for m in (mpesa_manual, mpesa_daraja, stars)}


def enabled():
    out = []
    for code in cfg.PAYMENT_METHODS:
        mod = PROVIDERS.get(code)
        if not mod:
            log.warning("Unknown payment method '%s'", code)
        elif not mod.available():
            log.warning("Payment method '%s' enabled but not fully configured — skipped", code)
        else:
            out.append(mod)
    return out
