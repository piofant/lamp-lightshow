import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	gridFromBpm, beatTime, beatAt, events, stepsFor, buildChunks, colorAt, autoShow,
	MAX_STEPS, SHARP_MS, PAD_MS, MAX_STEP_MS,
} from '../web/lamp-show.mjs';

const beats = gridFromBpm(120, 0.5, 60);          // доля = 0,5 с, первая на 0,5 с
const red = { h: 0, s: 100, b: 100, sharp: true };
const blue = { h: 235, s: 100, b: 100, sharp: true };

test('время доли и обратно сходятся, в том числе за краями', () => {
	for (const bf of [-1, 0, 2.5, 10.25, beats.length + 3]) {
		assert.ok(Math.abs(beatAt(beats, beatTime(beats, bf)) - bf) < 1e-6, `bf=${bf}`);
	}
	assert.equal(beatTime(beats, 1), 1.0);
});

test('смена темпа: время берётся из настоящих долей, а не из BPM', () => {
	const drift = [0, 0.5, 1.0, 1.4, 1.8];          // темп ускорился после второй доли
	assert.equal(beatTime(drift, 3), 1.4);
	assert.ok(Math.abs(beatAt(drift, 1.6) - 3.5) < 1e-9);
});

test('ни один кусок не длиннее 16 шагов, у каждого есть запасной шаг', () => {
	const cells = {};
	for (let i = 0; i < 64; i++) cells[i * 2] = i % 2 ? blue : red;
	const ch = buildChunks(cells, beats, 0, { cellQ: 2 });
	assert.ok(ch.length > 3);
	ch.forEach((c, i) => {
		assert.ok(c.steps.length <= MAX_STEPS, `шагов ${c.steps.length}`);
		const last = c.steps[c.steps.length - 1], prev = c.steps[c.steps.length - 2];
		assert.equal(last.d, PAD_MS);
		assert.equal(last.h, prev.h, 'запас держит последний цвет');
		// у финального куска последний цвет держится хвостом шоу, у остальных — ровно до следующей смены
		if (i < ch.length - 1) assert.ok(prev.d < 1000, 'последний настоящий шаг не растянут запасом');
	});
	for (let i = 1; i < ch.length; i++) assert.ok(ch[i].t > ch[i - 1].t, 'куски идут по порядку');
});

test('резкая клетка: короткий вход в цвет и удержание', () => {
	assert.deepEqual(stepsFor(red, 500, 250).map(s => s.d), [SHARP_MS, 500 - SHARP_MS]);
	assert.deepEqual(stepsFor(red, 120, 250).map(s => s.d), [120], 'короткую не дробим');
});

test('плавная клетка перетекает одну клетку, а дальше держит цвет', () => {
	const soft = { ...red, sharp: false };
	assert.deepEqual(stepsFor(soft, 4000, 500).map(s => s.d), [500, 3500]);
	assert.deepEqual(stepsFor(soft, 500, 500).map(s => s.d), [500]);
});

test('после перемотки первым идёт цвет, который уже горит, даже если дальше такой же', () => {
	const cells = { 0: red, 8: red, 16: blue };      // красный с доли 0, синий с доли 4
	const ev = events(cells, beats, beatTime(beats, 1));
	assert.equal(ev[0].c.h, 0);
	assert.ok(Math.abs(ev[0].t - beatTime(beats, 1)) < 1e-9);
	assert.equal(ev[1].c.h, 235);
	assert.equal(ev.length, 2, 'одинаковые подряд склеены');
});

test('пустой рисунок — пустое шоу, без падений', () => {
	assert.deepEqual(buildChunks({}, beats, 0), []);
	assert.equal(colorAt({}, beats, 5), null);
});

test('цвет в момент времени', () => {
	const cells = { 0: red, 8: blue };
	assert.equal(colorAt(cells, beats, 0.4), null, 'до первой клетки ничего');
	assert.equal(colorAt(cells, beats, 0.6).h, 0);
	assert.equal(colorAt(cells, beats, 1.6).h, 235);
});

test('автосвет: тихо — перелив раз в такт, мощно — вспышка и тьма, смена части — белая вспышка', () => {
	const cells = autoShow({ bars: 'lhm', events: [2] });
	assert.equal(cells[0].sharp, false);
	assert.equal(cells[4], undefined, 'в тихом такте на долях пусто');
	assert.equal(cells[16].b, 100);
	assert.equal(cells[18].b, 1, 'между долями в мощном такте тьма');
	assert.equal(cells[32].s, 0, 'начало новой части — белый');
	assert.equal(cells[32].b, 100);
	assert.equal(cells[36].sharp, true, 'средний такт — цвет на каждую долю');
	const hues = new Set(Object.values(autoShow({ bars: 'mmmm', events: [] })).map(c => c.h));
	assert.ok([...hues].every(h => [0, 28].includes(h)), 'внутри части — соседние оттенки, не каша');
});

test('автосвет на настоящем треке танцпола укладывается в лимиты лампы', async () => {
	const { default: data } = await import('../web/tracks.json', { with: { type: 'json' } });
	const tr = data.tracks.find(t => t.bars && t.beats);
	const ch = buildChunks(autoShow(tr), tr.beats, 0, { cellQ: 2 });
	assert.ok(ch.length > 10);
	assert.ok(ch.every(c => c.steps.length <= MAX_STEPS && c.steps.every(s => s.d >= 50)));
});

test('последний цвет держится до конца трека, а не крутит кусок по кругу', () => {
	const ch = buildChunks({ 0: red, 8: blue }, beats, 0, { endT: 60 });
	const steps = ch[ch.length - 1].steps, hold = steps[steps.length - 2];
	assert.equal(hold.h, 235);
	assert.ok(hold.d > 55000, `держим ${hold.d} мс`);
	const long = buildChunks({ 0: red }, beats, 0, { endT: 5000 });
	assert.ok(long[0].steps.every(s => s.d <= MAX_STEP_MS), 'не длиннее лимита посредника');
});

test('до первой клетки лампа тёмная, пустой рисунок — тоже тьма', () => {
	const ch = buildChunks({ 16: red }, beats, 0, { darkGap: true });
	assert.equal(ch[0].t, 0);
	assert.equal(ch[0].steps[0].b, 1, 'сначала тьма');
	assert.ok(ch[0].steps.some(s => s.h === 0 && s.b === 100), 'потом красный');
	const empty = buildChunks({}, beats, 3, { darkGap: true });
	assert.equal(empty.length, 1);
	assert.equal(empty[0].steps[0].b, 1);
});

test('кривой BPM не вешает вкладку', () => {
	assert.ok(gridFromBpm(-5, 0, 10).length > 0);
	assert.ok(gridFromBpm(0, 0, 10).length < 100);
	assert.ok(gridFromBpm(1e9, 0, 10).length < 100);
});
