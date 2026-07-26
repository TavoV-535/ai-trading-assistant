from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.core.clock import Clock, SimulatedClock, SystemClock


def test_system_clock_returns_timezone_aware_utc_now():
    clock = SystemClock()
    before = datetime.now(timezone.utc)
    now = clock.now()
    after = datetime.now(timezone.utc)

    assert now.tzinfo is not None
    assert before <= now <= after


def test_system_clock_is_a_clock():
    assert isinstance(SystemClock(), Clock)


def test_simulated_clock_defaults_to_a_fixed_epoch():
    # Deliberately not datetime.now() -- two SimulatedClock() instances
    # constructed on different real days/machines must still agree, which
    # is what makes two independent simulation runs produce identical
    # timestamps given identical configuration.
    clock_a = SimulatedClock()
    clock_b = SimulatedClock()
    assert clock_a.now() == clock_b.now() == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_simulated_clock_accepts_an_explicit_start():
    start = datetime(2023, 6, 1, 9, 30, tzinfo=timezone.utc)
    clock = SimulatedClock(start=start)
    assert clock.now() == start


def test_simulated_clock_advance_to_moves_time_forward():
    clock = SimulatedClock(start=datetime(2020, 1, 1, tzinfo=timezone.utc))
    new_time = datetime(2020, 1, 1, 0, 5, tzinfo=timezone.utc)
    clock.advance_to(new_time)
    assert clock.now() == new_time


def test_simulated_clock_advance_to_backwards_raises():
    clock = SimulatedClock(start=datetime(2020, 1, 1, 0, 10, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance_to(datetime(2020, 1, 1, 0, 5, tzinfo=timezone.utc))
    # The failed attempt must not have mutated the clock's internal state.
    assert clock.now() == datetime(2020, 1, 1, 0, 10, tzinfo=timezone.utc)


def test_simulated_clock_advance_to_same_instant_is_allowed():
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    clock = SimulatedClock(start=start)
    clock.advance_to(start)
    assert clock.now() == start


def test_simulated_clock_tick_advances_by_delta_and_returns_new_time():
    clock = SimulatedClock(start=datetime(2020, 1, 1, tzinfo=timezone.utc))
    returned = clock.tick(timedelta(seconds=60))
    assert returned == clock.now() == datetime(2020, 1, 1, 0, 1, tzinfo=timezone.utc)

    clock.tick(timedelta(seconds=60))
    assert clock.now() == datetime(2020, 1, 1, 0, 2, tzinfo=timezone.utc)


def test_simulated_clock_is_a_clock():
    assert isinstance(SimulatedClock(), Clock)
