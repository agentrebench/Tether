#ifndef TETHER_ORB_H
#define TETHER_ORB_H
#include <stdbool.h>
#include <stdint.h>
#define TT_ACTIVITY_LIMIT 16

typedef enum { TT_IDLE, TT_THINKING, TT_REASONING, TT_WORKING, TT_WAITING } tt_phase;
typedef struct { uint64_t id, sequence; tt_phase phase; } tt_activity_slot;
typedef struct {
    tt_activity_slot sessions[TT_ACTIVITY_LIMIT];
    double x, y, vx, vy, pulse, elapsed;
    unsigned score, combo;
    bool active, enabled, reduced_motion;
    tt_phase phase;
} tt_orb;
void tt_orb_init(tt_orb *orb);
bool tt_orb_activity(tt_orb *orb, uint64_t id, uint64_t sequence, const char *phase);
void tt_orb_remove(tt_orb *orb, uint64_t id);
void tt_orb_step(tt_orb *orb, double seconds);
bool tt_orb_tap(tt_orb *orb, double x, double y);
#endif
