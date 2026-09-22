#!/usr/bin/env node
/** Mixer state-machine test (Phase 3.7): the FULL transition table of
 *  the destructive-solo mixer (mixer.js) executed against a fake
 *  AudioContext — T1–T5, invariant I1 (never muted AND solo), snapshot
 *  restore, solo move, and the mix-duck rule.
 *
 *  Wired as `npm run test:mixer` and enforced in the CI frontend job.
 *  Pure Node: mixer.js imports nothing, so no DOM/WebAudio shims are
 *  needed beyond the tiny fakes below.
 */

import assert from 'node:assert/strict';
import { MixerState } from '../src/mixer.js';

// ------------------------------------------------------------ fakes
function fakeGainNode(sink) {
  const gain = {
    value: 1,
    target: null,
    setTargetAtTime(v) {
      this.target = v;
    },
  };
  return { gain, type: 'gain', connected: [],
           connect(p) { this.connected.push(p); sink.edges.push(this); },
           disconnect() {} };
}

function fakeCtx() {
  return {
    currentTime: 0,
    destination: { name: 'destination' },
    edges: [],
    createGain() { return fakeGainNode(this); },
  };
}

/** Build a mixer with 3 stem strips (A/B/C, default-muted like the
 *  panels' makeStrip) + the Mix row; returns {m, A, B, C, mix}. */
function rig() {
  const ctx = fakeCtx();
  const mixApplyCalls = [];
  const m = new MixerState(ctx, (norm) => mixApplyCalls.push(norm));
  const strips = ['A', 'B', 'C'].map((id) => m.makeStrip(id));
  m.route(strips);
  return {
    m, ctx, mixApplyCalls,
    A: strips[0], B: strips[1], C: strips[2], mix: m.mix,
  };
}

/** I1: no strip (Mix row included) is ever muted AND soloed. */
function invariantOk(m) {
  return m.all().every((s) => !(s.muted && s.solo));
}

const S = (m, s) => m.pressSolo(s); // press the S button
const M = (m, s) => m.pressMute(s); // press the M button

let passed = 0;
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`  ok  ${name}`);
}

console.log('mixer transition table (T1–T5, I1, snapshot, mix duck)');

test('T1: S on A engages destructive solo', () => {
  const { m, A, B, C, mix } = rig();
  S(m, A);
  assert.equal(A.solo, true);
  assert.equal(A.muted, false, 'T5 inside T1: solo unmutes A');
  for (const s of [B, C, mix]) {
    assert.equal(s.muted, true, 'every other row force-muted');
    assert.equal(s.solo, false);
  }
  assert.ok(invariantOk(m));
  m.apply();
  assert.equal(A.gain.gain.target, 1);
  assert.equal(B.gain.gain.target, 0);
});

test('T2: S on A again releases and restores the snapshot', () => {
  const { m, A, B, C, mix } = rig();
  S(m, A);
  S(m, A); // release
  assert.equal(A.solo, false);
  assert.equal(A.muted, false, 'T2 leaves the releasing soloist audible');
  assert.equal(B.muted, true, 'stems restored to their born-muted state');
  assert.equal(C.muted, true);
  assert.equal(mix.muted, false, 'mix restored to pre-solo unmuted');
  assert.ok(invariantOk(m));
});

test('T2 restores USER mutes, not just defaults', () => {
  const { m, A, B } = rig();
  M(m, B); // user unmutes B (B was default-muted)
  assert.equal(B.muted, false);
  S(m, A); // solo A (B force-muted)
  assert.equal(B.muted, true);
  S(m, A); // release
  assert.equal(B.muted, false, 'user unmute survives the solo cycle');
});

test('T3: unmuting a row while X soloed releases the solo first', () => {
  const { m, A, B, mix } = rig();
  M(m, B); // user unmutes B
  S(m, A); // A solo: B force-muted, mix force-muted
  M(m, B); // unmute press on a solo-forced-muted row
  assert.equal(A.solo, false, 'solo released');
  assert.equal(A.muted, false, 'released soloist keeps its own mute (audible)');
  assert.equal(B.muted, false, 'Drums-equivalent ends audible');
  assert.equal(mix.muted, false, 'mix restored');
  assert.ok(invariantOk(m));
  m.apply();
  assert.equal(B.gain.gain.target, 1);
  assert.ok(A.gain.gain.target > 0, 'A still audible after the release');
});

test('T4: M on an unmuted non-solo row mutes it', () => {
  const { m, A, B } = rig();
  M(m, A); // unmute (born muted)
  assert.equal(A.muted, false);
  M(m, A); // mute press
  assert.equal(A.muted, true);
  assert.equal(A.solo, false);
  assert.equal(B.muted, true);
  assert.ok(invariantOk(m));
});

test('T4: M on the soloist releases its solo, then mutes it', () => {
  const { m, A, B, mix } = rig();
  M(m, B); // B audible pre-solo (user unmute)
  S(m, A);
  M(m, A); // mute press on the unmuted soloist
  assert.equal(A.solo, false);
  assert.equal(A.muted, true, 'ends muted, never muted+solo');
  assert.equal(B.muted, false, 'others restored from snapshot');
  assert.equal(mix.muted, false);
  assert.ok(invariantOk(m));
});

test('T5: S on a muted strip unmutes it, then solos it', () => {
  const { m, A } = rig();
  assert.equal(A.muted, true);
  S(m, A);
  assert.equal(A.muted, false);
  assert.equal(A.solo, true);
  assert.ok(invariantOk(m));
});

test('solo MOVE: S on B while A soloed hands the solo over', () => {
  const { m, A, B, C, mix } = rig();
  M(m, A); // user unmutes A (A ends audible pre-solo)
  S(m, A); // A solo
  S(m, B); // move to B
  assert.equal(B.solo, true);
  assert.equal(B.muted, false);
  assert.equal(A.solo, false);
  assert.equal(A.muted, true, 'previous soloist force-muted');
  assert.equal(C.muted, true);
  assert.equal(mix.muted, true);
  S(m, B); // release — restores the FROZEN pre-solo snapshot
  assert.equal(A.muted, false, 'user unmute of A survives the move');
  assert.equal(mix.muted, false);
  assert.equal(B.muted, false, 'releasing soloist stays audible');
  assert.ok(invariantOk(m));
});

test('I1: any click sequence leaves muted AND solo unrepresentable', () => {
  const { m, A, B, C, mix } = rig();
  const presses = [
    [M, A], [S, A], [S, B], [M, C], [S, C], [M, A], [M, mix],
    [S, mix], [S, B], [M, B], [S, A], [S, A], [M, C], [M, C],
    [S, B], [M, mix], [S, mix], [M, A], [S, C], [M, C], [S, C],
  ];
  for (const [fn, s] of presses) {
    fn(m, s);
    assert.ok(invariantOk(m), `invariant after ${fn === S ? 'S' : 'M'} on ${s.id}`);
  }
  assert.ok(m.all().some((s) => !s.muted));
});

test('mix-duck rule: the Mix plays only while effectively alone', () => {
  const { m, A, mixApplyCalls } = rig();
  m.apply();
  assert.ok(mixApplyCalls.at(-1) > 0, 'all stems muted: mix audible');
  M(m, A); // unmute one stem
  m.apply();
  assert.equal(mixApplyCalls.at(-1), 0, 'a stem is open: mix ducked');
  S(m, A); // solo A (A stays open, others muted)
  m.apply();
  assert.equal(mixApplyCalls.at(-1), 0, 'solo of a stem ducks the mix');
  M(m, A); // T4: mute the soloist -> solo released, ALL stems muted
  m.apply();
  assert.ok(mixApplyCalls.at(-1) > 0, 'no stem open: mix back');
});

test('mix solo: the Mix can solo itself above every stem', () => {
  const { m, A, B, mix } = rig();
  M(m, A); // open a stem (mix ducks)
  S(m, mix); // solo the Mix row
  assert.equal(mix.solo, true);
  for (const s of [A, B]) assert.equal(s.muted, true);
  assert.ok(m.mixAudible());
  S(m, mix);
  assert.equal(A.muted, false, 'stem mutes restored after mix solo');
  assert.ok(invariantOk(m));
});

console.log(`test:mixer OK: ${passed} cases green`);
