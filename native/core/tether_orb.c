#include "tether_orb.h"
#include <math.h>
#include <stddef.h>
#include <string.h>

static void aggregate(tt_orb *orb) {
    tt_phase phase = TT_IDLE;
    for (size_t i = 0; i < TT_ACTIVITY_LIMIT; ++i) {
        tt_phase p = orb->sessions[i].phase;
        if (orb->sessions[i].id && (p == TT_REASONING || (p == TT_THINKING && phase != TT_REASONING))) phase = p;
    }
    bool active = orb->enabled && (phase == TT_THINKING || phase == TT_REASONING);
    if (active && !orb->active) { orb->combo = 0; orb->pulse = 1; }
    orb->phase = phase;
    orb->active = active;
}

void tt_orb_init(tt_orb *orb) {
    *orb = (tt_orb){ .x = 120, .y = 90, .vx = 64, .vy = 39, .enabled = true };
}

bool tt_orb_activity(tt_orb *orb, uint64_t id, uint64_t sequence, const char *phase) {
    if (!id || !sequence || !phase) return false;
    const char *names[] = { "idle", "thinking", "reasoning", "working", "waiting" };
    int parsed = -1;
    for (int i = 0; i < 5; ++i) if (!strcmp(phase, names[i])) parsed = i;
    if (parsed < 0) return false;
    tt_activity_slot *slot = NULL;
    for (size_t i = 0; i < TT_ACTIVITY_LIMIT; ++i) if (orb->sessions[i].id == id) slot = &orb->sessions[i];
    if (!slot) for (size_t i = 0; i < TT_ACTIVITY_LIMIT; ++i) if (!orb->sessions[i].id) { slot = &orb->sessions[i]; break; }
    if (!slot || (slot->id == id && sequence <= slot->sequence)) return false;
    *slot = (tt_activity_slot){id, sequence, (tt_phase)parsed};
    aggregate(orb);
    return true;
}

void tt_orb_remove(tt_orb *orb, uint64_t id) {
    for (size_t i = 0; i < TT_ACTIVITY_LIMIT; ++i) if (orb->sessions[i].id == id) orb->sessions[i] = (tt_activity_slot){0};
    aggregate(orb);
}

void tt_orb_step(tt_orb *orb, double seconds) {
    aggregate(orb);
    if (!orb->active || !isfinite(seconds) || seconds <= 0) return;
    double dt = fmin(seconds, .05);
    orb->elapsed += dt;
    orb->pulse = fmax(0, orb->pulse - dt * 2);
    if (orb->reduced_motion) return;
    orb->x += orb->vx * dt;
    orb->y += orb->vy * dt;
    if (orb->x < 28) { orb->x = 28; orb->vx = fabs(orb->vx); }
    if (orb->x > 232) { orb->x = 232; orb->vx = -fabs(orb->vx); }
    if (orb->y < 36) { orb->y = 36; orb->vy = fabs(orb->vy); }
    if (orb->y > 150) { orb->y = 150; orb->vy = -fabs(orb->vy); }
}

bool tt_orb_tap(tt_orb *orb, double x, double y) {
    if (!orb->active || !isfinite(x) || !isfinite(y)) return false;
    double dx = x - orb->x, dy = y - orb->y;
    if (dx * dx + dy * dy > 34 * 34) { orb->combo = 0; return false; }
    orb->combo = orb->combo < 99 ? orb->combo + 1 : 99;
    unsigned added = 10 * orb->combo;
    orb->score = orb->score <= 999999 - added ? orb->score + added : 999999;
    double speed = fmin(170, 68 + orb->combo * 5);
    orb->vx = (dx >= 0 ? -1 : 1) * speed;
    orb->vy = (dy >= 0 ? -1 : 1) * speed * .65;
    orb->pulse = 1;
    return true;
}
