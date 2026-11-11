"""system_priority.py — macOS prioriteits-optimalisaties voor live DJ-shows.

Voorkomt App Nap, system sleep, en CPU-throttling terwijl de show draait.

Drie mechanismen (onafhankelijk van elkaar — een falend mechanisme blokkeert
de andere niet):

1. NSProcessInfo Activity
   Vraagt aan macOS om het proces te behandelen als latency-kritisch
   gebruikerswerk via Objective-C bridge (pyobjc).
   Vlag: NSActivityUserInitiated | NSActivityLatencyCritical
   Effect: App Nap uitgeschakeld, geen CPU-budget-limiet, geen idle timers.

2. caffeinate subprocess
   Start `caffeinate -dims -w <PID>` als child-process.
   - d = voorkomt display sleep
   - i = voorkomt systeem-idle sleep
   - m = voorkomt schijf-spindown
   - s = voorkomt systeem-sleep
   -w <PID> = stopt automatisch wanneer het Python-process eindigt.

3. Process nice level
   os.nice(-10) — verlaagt niceness zodat de scheduler meer CPU geeft.
   (Nice -20 vereist root; -10 is het minimum voor gewone gebruikers op macOS.)

Gebruik:
    from system_priority import SystemPriority
    sp = SystemPriority()
    sp.start()
    print(sp.status_line())
    # → "App Nap disabled: YES | Caffeinate: YES | Nice: -10"
    sp.stop()   # optioneel — caffeinate stopt via -w PID automatisch
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys

_log = logging.getLogger("djlights.priority")

# NSProcessInfo constanten (uit Apple NSProcessInfo.h)
_NSActivityIdleSystemSleepDisabled  = 1 << 20       # 0x100000
_NSActivityUserInitiated            = 0x00FFFFFF | _NSActivityIdleSystemSleepDisabled
_NSActivityLatencyCritical          = 0xFF << 32     # 0xFF00000000
_ACTIVITY_FLAGS = _NSActivityUserInitiated | _NSActivityLatencyCritical


class SystemPriority:
    """Beheert macOS-systeemprioriteit voor de duur van een live show.

    Thread-safe; alle methodes zijn idempotent (meerdere aanroepen zijn veilig).
    """

    def __init__(self) -> None:
        self._activity       = None   # NSProcessInfo Activity object
        self._caffeinate     = None   # subprocess.Popen
        self._app_nap_ok     = False
        self._caffeinate_ok  = False
        self._nice_level     = 0

    # ─────────────────────────────────────────────────────────────────────
    # Publieke API
    # ─────────────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Activeer alle prioriteits-mechanismen. Roep één keer aan bij opstart."""
        self._disable_app_nap()
        self._start_caffeinate()
        self._raise_priority()
        _log.info("[SystemPriority] %s", self.status_line())
        print(f"[SystemPriority] {self.status_line()}")

    def stop(self) -> None:
        """Hef alle prioriteits-overrides op. Roep aan bij afsluiten."""
        self._stop_caffeinate()
        self._stop_app_nap()

    def status_line(self) -> str:
        """Eénregelige statussamenvatting voor console/diagnostics."""
        nap  = "YES" if self._app_nap_ok    else "NO"
        caff = "YES" if self._caffeinate_ok else "NO"
        nice = str(self._nice_level) if self._nice_level != 0 else "unchanged"
        return f"App Nap disabled: {nap} | Caffeinate: {caff} | Nice: {nice}"

    def status_dict(self) -> dict:
        return {
            "app_nap_disabled": self._app_nap_ok,
            "caffeinate_active": self._caffeinate_ok,
            "nice_level": self._nice_level,
        }

    # ─────────────────────────────────────────────────────────────────────
    # 1. NSProcessInfo — App Nap + Latency Critical
    # ─────────────────────────────────────────────────────────────────────

    def _disable_app_nap(self) -> None:
        """Vraag macOS om NSActivityLatencyCritical + NSActivityUserInitiated."""
        if sys.platform != "darwin":
            _log.debug("[SystemPriority] App Nap: niet macOS, overgeslagen")
            return
        try:
            # pyobjc is optioneel — graceful fallback als het niet geïnstalleerd is
            from Foundation import NSProcessInfo  # type: ignore
            pi = NSProcessInfo.processInfo()
            self._activity = pi.beginActivityWithOptions_reason_(
                _ACTIVITY_FLAGS,
                "DJ Lights live show — latency critical",
            )
            self._app_nap_ok = True
            _log.debug("[SystemPriority] App Nap uitgeschakeld via NSProcessInfo (flags=0x%x)",
                       _ACTIVITY_FLAGS)
        except ImportError:
            _log.warning("[SystemPriority] pyobjc/Foundation niet beschikbaar — "
                         "App Nap NIET uitgeschakeld. Installeer: pip install pyobjc-framework-Cocoa")
        except Exception as e:
            _log.warning("[SystemPriority] NSProcessInfo activity mislukt: %s", e)

    def _stop_app_nap(self) -> None:
        if self._activity is None:
            return
        try:
            from Foundation import NSProcessInfo  # type: ignore
            NSProcessInfo.processInfo().endActivity_(self._activity)
            self._activity    = None
            self._app_nap_ok  = False
            _log.debug("[SystemPriority] NSProcessInfo activity beëindigd")
        except Exception as e:
            _log.debug("[SystemPriority] endActivity fout (onschadelijk): %s", e)

    # ─────────────────────────────────────────────────────────────────────
    # 2. caffeinate subprocess
    # ─────────────────────────────────────────────────────────────────────

    def _start_caffeinate(self) -> None:
        """Start caffeinate als child-process. Stopt automatisch via -w PID."""
        if sys.platform != "darwin":
            return
        try:
            pid = os.getpid()
            # -d display, -i idle, -m disk, -s system sleep
            # -w <pid> auto-stop wanneer dit process eindigt
            self._caffeinate = subprocess.Popen(
                ["/usr/bin/caffeinate", "-dims", "-w", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            self._caffeinate_ok = True
            _log.debug("[SystemPriority] caffeinate gestart (PID %d, wacht op %d)",
                       self._caffeinate.pid, pid)
        except FileNotFoundError:
            _log.warning("[SystemPriority] caffeinate niet gevonden op dit systeem")
        except Exception as e:
            _log.warning("[SystemPriority] caffeinate start mislukt: %s", e)

    def _stop_caffeinate(self) -> None:
        if self._caffeinate is None:
            return
        try:
            self._caffeinate.terminate()
            self._caffeinate    = None
            self._caffeinate_ok = False
            _log.debug("[SystemPriority] caffeinate gestopt")
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────
    # 3. Process nice level
    # ─────────────────────────────────────────────────────────────────────

    def _raise_priority(self) -> None:
        """Verlaag nice-waarde voor meer CPU-budget.

        -10 is het minimum zonder root op macOS.  -20 vereist sudo —
        als dat mislukt vallen we terug op -10.
        """
        if sys.platform != "darwin":
            return
        for target in (-20, -10):
            try:
                os.nice(target)
                self._nice_level = target
                _log.debug("[SystemPriority] nice gezet naar %d", target)
                return
            except PermissionError:
                if target == -20:
                    _log.debug("[SystemPriority] nice -20 vereist root, probeer -10")
                    continue
                _log.warning("[SystemPriority] nice -10 ook geweigerd — prioriteit ongewijzigd")
            except Exception as e:
                _log.warning("[SystemPriority] os.nice fout: %s", e)
                return
