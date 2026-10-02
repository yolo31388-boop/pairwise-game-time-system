"""GameTimeSystem - unified game clock.

Single source of truth for time in the game:

- ``update_time()``   : scaled, pause-aware, fixed-timestep simulation
- ``add_timer()``     : game-time timers (scaled, pause-aware, deduplicated)
- ``update_day_night()``: smooth day/night blend, sun position, events
- ``sync_time()``     : latency-compensated, rate-limited, interpolated sync
- ``save_time()``/``load_time()``: real-time aware, validated, offline progress
"""

import hashlib
import json
import math
import time


def _smoothstep(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


class GameTimeSystem:
    DAY_LENGTH = 240.0              # game seconds per full day/night cycle
    FIXED_TIMESTEP = 1.0 / 60.0     # logic step, frame-rate independent
    SYNC_INTERVAL = 1.0             # min real seconds between accepted syncs
    SYNC_LERP = 0.5                 # offset smoothing factor per sync
    MAX_OFFLINE_SECONDS = 8 * 3600  # cap on offline time progression
    TWILIGHT_BAND = 0.25            # sun-elevation band for the day/night blend
    CLOCK_SKEW_TOLERANCE = 60.0     # allowed future drift of saved real time

    def __init__(self, time_scale=1.0, fixed_timestep=FIXED_TIMESTEP,
                 day_length=DAY_LENGTH):
        self.state = {}

        # clock
        self.time_scale = time_scale
        self.paused = False
        self.fixed_timestep = fixed_timestep
        self.game_time = 0.0
        self._accumulator = 0.0

        # timers
        self.timers = []
        self._timer_keys = set()

        # day/night
        self.day_length = day_length
        self.sun_elevation = 0.0
        self.sun_azimuth = 0.0
        self.day_blend = 1.0          # 1.0 = full day, 0.0 = full night
        self.is_night = False
        self.events = []
        self._listeners = {}

        # network sync
        self.time_offset = 0.0        # smoothed server-minus-client offset
        self._target_offset = 0.0
        self._last_sync_real = None
        self._sync_count = 0
        self.sync_interval = self.SYNC_INTERVAL

        self._save_secret = "game-time-system-v1"

    # ---------------------------------------------------------------- clock

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def set_time_scale(self, scale):
        self.time_scale = max(0.0, float(scale))

    def update_time(self, dt=None, time_scale=None):
        """Advance game time by ``dt`` real seconds.

        The delta is scaled by the current time scale, ignored while
        paused, and consumed in fixed steps so logic speed is
        independent of frame rate.
        """
        if dt is None:
            dt = self.fixed_timestep
        if time_scale is not None:
            self.set_time_scale(time_scale)
        if self.paused:
            return self.game_time
        self._accumulator += dt * self.time_scale
        while self._accumulator >= self.fixed_timestep:
            self._accumulator -= self.fixed_timestep
            self._step(self.fixed_timestep)
        self.update_day_night()
        return self.game_time

    def _step(self, step):
        self.game_time += step
        self._update_timers(step)

    # --------------------------------------------------------------- timers

    def add_timer(self, duration=1.0, callback=None, name=None, repeat=False):
        """Register a timer that counts down in scaled, pausable game time.

        Timers are deduplicated by ``name`` (or by ``callback`` when no
        name is given): registering the same key twice returns the
        existing timer instead of stacking a duplicate.
        """
        key = name if name is not None else callback
        if key is not None and key in self._timer_keys:
            for timer in self.timers:
                if timer["key"] == key:
                    return timer
        timer = {
            "key": key,
            "duration": float(duration),
            "remaining": float(duration),
            "callback": callback,
            "repeat": repeat,
        }
        self.timers.append(timer)
        if key is not None:
            self._timer_keys.add(key)
        return timer

    def _update_timers(self, step):
        fired = []
        for timer in list(self.timers):
            timer["remaining"] -= step
            if timer["remaining"] > 0:
                continue
            if timer["callback"] is not None:
                timer["callback"]()
            fired.append(timer)
        for timer in fired:
            if timer["repeat"]:
                timer["remaining"] += timer["duration"]
            else:
                self.timers.remove(timer)
                if timer["key"] is not None:
                    self._timer_keys.discard(timer["key"])

    # ------------------------------------------------------------ day/night

    def on(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def _trigger(self, event, info):
        self.events.append(event)
        for callback in self._listeners.get(event, []):
            callback(info)

    def update_day_night(self):
        """Recompute the day/night state from game time.

        The day/night blend is a smooth function of sun elevation (no
        snapping between day and night), the sun position advances with
        game time, and ``day_started``/``night_started`` events fire on
        transitions.
        """
        t = (self.game_time % self.day_length) / self.day_length
        self.sun_elevation = math.sin(2.0 * math.pi * t)
        self.sun_azimuth = (t * 360.0 + 180.0) % 360.0
        band = self.TWILIGHT_BAND
        self.day_blend = _smoothstep((self.sun_elevation + band) / (2.0 * band))

        was_night = self.is_night
        self.is_night = self.sun_elevation < 0.0
        info = {
            "cycle_position": t,
            "sun_elevation": self.sun_elevation,
            "sun_azimuth": self.sun_azimuth,
            "day_blend": self.day_blend,
            "is_night": self.is_night,
        }
        if self.is_night and not was_night:
            self._trigger("night_started", info)
        elif was_night and not self.is_night:
            self._trigger("day_started", info)
        return info

    # ----------------------------------------------------------------- sync

    def sync_time(self, server_time=None, rtt=0.0, now=None):
        """Sync the client clock to the server.

        Compensates for one-way latency (``rtt / 2``), ignores syncs
        arriving faster than ``sync_interval`` (rate control), and
        interpolates the offset toward its target so the client clock
        never jumps.
        """
        if now is None:
            now = time.time()
        if server_time is None:
            server_time = self.game_time
        if (self._last_sync_real is not None
                and now - self._last_sync_real < self.sync_interval):
            return self.game_time + self.time_offset
        self._last_sync_real = now
        compensated = server_time + rtt / 2.0
        self._target_offset = compensated - self.game_time
        if self._sync_count == 0:
            self.time_offset = self._target_offset
        else:
            self.time_offset += (self._target_offset - self.time_offset) * self.SYNC_LERP
        self._sync_count += 1
        return self.game_time + self.time_offset

    def get_synced_time(self, blend=0.1):
        """Per-frame interpolated server-time estimate (no snapping)."""
        self.time_offset += (self._target_offset - self.time_offset) * blend
        return self.game_time + self.time_offset

    # --------------------------------------------------------- persistence

    def _checksum(self, payload):
        data = {k: v for k, v in payload.items() if k != "checksum"}
        raw = json.dumps(data, sort_keys=True) + self._save_secret
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def save_time(self, now=None):
        """Persist game time together with real time and timezone."""
        if now is None:
            now = time.time()
        local = time.localtime(now)
        tz_offset = getattr(local, "tm_gmtoff", None)
        if tz_offset is None:
            tz_offset = -time.timezone
        payload = {
            "version": 1,
            "game_time": self.game_time,
            "real_time": now,
            "timezone_offset": tz_offset,
            "time_scale": self.time_scale,
        }
        payload["checksum"] = self._checksum(payload)
        self.state["save"] = payload
        return payload

    def load_time(self, save, now=None):
        """Restore a save, validating it and applying offline progress.

        Rejects tampered payloads (checksum mismatch) and saves whose
        real time lies in the future (local clock manipulation). Game
        time advances for the period the player was offline, capped at
        ``MAX_OFFLINE_SECONDS``.
        """
        if now is None:
            now = time.time()
        if not isinstance(save, dict) or save.get("checksum") != self._checksum(save):
            raise ValueError("invalid or tampered time save")
        saved_real = save["real_time"]
        if saved_real > now + self.CLOCK_SKEW_TOLERANCE:
            raise ValueError("saved real time is in the future: clock tampering")
        offline = min(max(0.0, now - saved_real), self.MAX_OFFLINE_SECONDS)
        self.time_scale = save.get("time_scale", self.time_scale)
        self.game_time = save["game_time"] + offline * self.time_scale
        self.update_day_night()
        return self.game_time
