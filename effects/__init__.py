from .intro        import EFFECTS as INTRO_EFFECTS
from .verse        import EFFECTS as VERSE_EFFECTS
from .chorus       import EFFECTS as CHORUS_EFFECTS
from .up           import EFFECTS as UP_EFFECTS
from .down         import EFFECTS as DOWN_EFFECTS
from .outro        import EFFECTS as OUTRO_EFFECTS
from .choreo_modes import EFFECTS as CHOREO_EFFECTS
# Bridge/vault/advanced kept for AI plan backward-compat but excluded from random pool
from .bridge   import EFFECTS as BRIDGE_EFFECTS
from .vault    import EFFECTS as VAULT_EFFECTS
from .advanced import EFFECTS as ADVANCED_EFFECTS

CATEGORIES = [
    ("INTRO",  INTRO_EFFECTS),
    ("VERSE",  VERSE_EFFECTS),
    ("CHORUS", CHORUS_EFFECTS),
    ("UP",     UP_EFFECTS),
    ("DOWN",   DOWN_EFFECTS),
    ("OUTRO",  OUTRO_EFFECTS),
    ("CHOREO", CHOREO_EFFECTS),
]

ALL_EFFECTS = [e for _, effects in CATEGORIES for e in effects]
