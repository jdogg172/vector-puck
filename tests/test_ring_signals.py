"""Fresh weather selection, bounded light leases and native compatibility."""
import unittest

from vector_puck.ring_signals import IndicatorLease, native_weather, weather_indicator
from vector_puck.update_indicator import choose_indicator


def alert(event, start=900, end=2000):
    return dict(event=event, effective_at=start, expires_at=end)


def snapshot(*items, valid=1300, **extra):
    return dict(available=True, stale=False, valid_until=valid, data={"items": list(items)}, **extra)


class RingSignalTests(unittest.TestCase):
    def test_weather_severity_and_flood_sequences(self):
        for event, kind in [("Tornado Warning", "warning"), ("Severe Thunderstorm Watch", "watch"),
                            ("Wind Advisory", "advisory"), ("Flood Watch", "flood-watch"),
                            ("Flash Flood Warning", "flood-warning"), ("Flood Advisory", "flood-advisory")]:
            with self.subTest(event=event):
                self.assertEqual(weather_indicator(snapshot(alert(event)), now=1000),
                                 dict(kind=kind, duration_ms=60000))

    def test_severity_wins_regardless_of_feed_order(self):
        tornado = alert("Tornado Warning")
        flood = alert("Flash Flood Warning")
        watch = alert("Tornado Watch")
        self.assertEqual(weather_indicator(snapshot(flood, watch, tornado), now=1000)["kind"], "warning")
        self.assertEqual(weather_indicator(snapshot(tornado, watch, flood), now=1000)["kind"], "warning")
        self.assertEqual(weather_indicator(snapshot(watch, flood), now=1000)["kind"], "flood-warning")

    def test_feed_validity_and_alert_expiry_both_bound_device_lease(self):
        self.assertEqual(weather_indicator(snapshot(alert("Flood Watch"), valid=1007), now=1000)["duration_ms"], 7000)
        self.assertEqual(weather_indicator(snapshot(alert("Flood Watch", end=1003)), now=1000)["duration_ms"], 3000)
        self.assertEqual(weather_indicator(snapshot(alert("Flood Watch", end=1000)), now=1000), {"kind": "none"})
        self.assertEqual(weather_indicator(snapshot(alert("Flood Watch"), valid=1000), now=1000), {"kind": "none"})

    def test_invalid_stale_future_or_unknown_data_does_not_claim_an_alert(self):
        values = [None, [], {}, snapshot(), snapshot(alert("Flood Watch", start=1001)),
                  snapshot(alert("Flood Watch", end=float("inf"))), snapshot(alert("Flood Watch", start=True)),
                  snapshot(alert("Flood Watch"), valid=float("nan")), snapshot(alert("Weather Alert")),
                  dict(snapshot(alert("Flood Watch")), stale=True),
                  dict(snapshot(alert("Flood Watch")), available=False),
                  dict(snapshot(alert("Flood Watch")), data={"items": "Flood Watch"})]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(weather_indicator(value, now=1000), {"kind": "none"})

    def test_native_compatibility_keeps_severity(self):
        source = dict(kind="flood-warning", duration_ms=20000)
        self.assertEqual(native_weather(source, capable=True), source)
        self.assertEqual(native_weather(source, capable=False), dict(kind="warning", duration_ms=20000))

    def test_notification_and_update_never_hide_weather(self):
        for kind in ["warning", "watch", "advisory", "flood-warning", "flood-watch", "flood-advisory"]:
            weather = dict(kind=kind, duration_ms=30000)
            self.assertEqual(choose_indicator(weather=weather, call=True, alarm=True, messages=True,
                                              update={"kind": "update"}), weather)
        self.assertEqual(choose_indicator(messages=True, update={"kind": "updated"}),
                         dict(kind="message", duration_ms=60000))
        self.assertEqual(choose_indicator(weather={"kind": "none"}, call=True, messages=True), {"kind": "none"})

    def test_lease_renewal_does_not_restart_every_tick(self):
        lease = IndicatorLease()
        command = dict(kind="flood-watch", duration_ms=60000)
        self.assertTrue(lease.due(command, now=100))
        lease.sent(command, now=100)
        self.assertFalse(lease.due(command, now=100.5))
        self.assertFalse(lease.due(command, now=119))
        self.assertTrue(lease.due(command, now=120))
        self.assertTrue(lease.due({"kind": "none"}, now=101))
        self.assertTrue(lease.due(dict(kind="warning", duration_ms=60000), now=101))

    def test_short_lease_and_clear_after_feed_expires(self):
        lease = IndicatorLease()
        source = snapshot(alert("Flood Watch"), valid=1006)
        command = weather_indicator(source, now=1000)
        lease.sent(command, now=10)
        self.assertFalse(lease.due(command, now=12))
        self.assertTrue(lease.due(command, now=13))
        # Same cached snapshot later; renewal cannot extend its weather validity.
        self.assertEqual(weather_indicator(source, now=1005)["duration_ms"], 1000)
        cleared = weather_indicator(source, now=1006)
        self.assertEqual(cleared, {"kind": "none"})
        self.assertTrue(lease.due(cleared, now=16))
        lease.sent(cleared, now=16)
        self.assertFalse(lease.due(cleared, now=500))


class RuntimeSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_room_worker_emits_flood_signal_or_legacy_severity(self):
        import asyncio
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from vector_puck.household_runtime import RoomRuntime
        for capable, wanted in [(True, 'flood-watch'), (False, 'watch')]:
            with self.subTest(capable=capable):
                stopped = asyncio.Event()
                sent = []
                async def send(kind, data):
                    sent.append((kind, data))
                    stopped.set()
                satellite = SimpleNamespace(connected=True, stop=stopped, epoch=0,
                    allowed_base=lambda epoch: True, busy=False, speaking=False, recording=False,
                    followup=SimpleNamespace(active=lambda: False, ready=False), activity='idle',
                    native_indicator=True, native_weather_indicator=capable, native_update_indicator=True,
                    send=send)
                household = SimpleNamespace(heartbeat=lambda **kw: None, export_public=lambda: None,
                    call=lambda: None, pending_messages=lambda: 2, owns_turn=lambda: False)
                runtime = RoomRuntime.__new__(RoomRuntime)
                runtime.satellite = satellite
                runtime.household = household
                runtime.active = None
                runtime.alarms = None
                runtime.sync_alarms = AsyncMock()
                runtime.indicator_lease = IndicatorLease()
                runtime.set_weather_snapshot(snapshot(alert('Flood Watch'), valid=time.time()+40))
                # Replace only alert time bounds; feed and alert clocks must both be valid.
                runtime.weather_snapshot['data']['items'][0].update(effective_at=time.time()-1, expires_at=time.time()+60)
                await runtime.maintain()
                self.assertEqual(sent[0][0], 'vector-indicator')
                self.assertEqual(sent[0][1]['kind'], wanted)
                self.assertLessEqual(sent[0][1]['duration_ms'], 40000)
                self.assertFalse(hasattr(satellite, 'hardware_muted'))  # no remote privacy mutation

    async def test_spoken_explanation_uses_live_snapshot_and_privacy_priority(self):
        import time
        from types import SimpleNamespace
        from vector_puck.ambient_runtime import AmbientRuntime
        room = SimpleNamespace(weather_snapshot=snapshot(alert('Flash Flood Warning', start=time.time()-1,
            end=time.time()+60), valid=time.time()+30), household=SimpleNamespace(call=lambda: None,
            pending_messages=lambda: 0))
        satellite = SimpleNamespace(hardware_muted=False, room_runtime=room)
        essentials = SimpleNamespace(notices=SimpleNamespace(pending=lambda: 0))
        runtime = AmbientRuntime(essentials, satellite)
        self.assertIn('alternating red and blue', runtime.describe_ring())
        satellite.hardware_muted = True
        self.assertIn('microphone is muted', runtime.describe_ring())
        satellite.hardware_muted = False
        room.weather_snapshot['stale'] = True
        self.assertNotIn('There is a flood warning', runtime.describe_ring())


if __name__ == "__main__":
    unittest.main()
