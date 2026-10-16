"""Public fixture distribution has no private Gamma replay configuration."""


class GammaBindingUnavailable(RuntimeError):
    pass


def load_configured_gamma_provider():
    return None


def load_lazy_configured_gamma_provider():
    return None
