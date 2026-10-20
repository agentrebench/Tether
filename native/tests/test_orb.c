#include "tether_orb.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
int main(void) {
    tt_orb o; tt_orb_init(&o);
    assert(!o.active && !tt_orb_tap(&o, 120, 90));
    assert(tt_orb_activity(&o, 1, 1, "thinking") && o.active);
    assert(!tt_orb_activity(&o, 1, 1, "idle") && o.active);
    assert(!tt_orb_activity(&o, 1, 2, "invalid") && o.active);
    assert(tt_orb_activity(&o, 1, 2, "reasoning") && o.phase == TT_REASONING);
    assert(tt_orb_tap(&o, o.x, o.y) && o.score == 10 && o.combo == 1);
    o.score = 999890; o.combo = 98;
    assert(tt_orb_tap(&o, o.x, o.y) && o.score == 999999 && o.combo == 99);
    assert(tt_orb_tap(&o, o.x, o.y) && o.score == 999999);
    assert(!tt_orb_tap(&o, NAN, 0));
    for (int i = 0; i < 10000; ++i) {
        tt_orb_step(&o, 1.0 / 60);
        assert(o.x >= 28 && o.x <= 232 && o.y >= 36 && o.y <= 150);
    }
    o.reduced_motion = true; double x = o.x;
    tt_orb_step(&o, .02); assert(x == o.x);
    assert(tt_orb_activity(&o, 2, 1, "thinking"));
    assert(tt_orb_activity(&o, 1, 3, "waiting") && o.active);
    tt_orb_remove(&o, 2); assert(!o.active);
    assert(tt_orb_activity(&o, 1, 4, "working") && !o.active);
    assert(tt_orb_activity(&o, 1, 5, "thinking") && o.active);
    tt_orb_remove(&o, 1); assert(!o.active);
    for (unsigned i = 1; i <= TT_ACTIVITY_LIMIT; ++i) assert(tt_orb_activity(&o, i, 1, "thinking"));
    assert(!tt_orb_activity(&o, 100, 1, "thinking"));
    o.enabled = false; tt_orb_step(&o, .02); assert(!o.active);
    puts("orb: phase gating, stale events, cancellation, bounds, taps, reduced motion passed");
}
