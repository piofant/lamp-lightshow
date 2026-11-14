/**
 * Светошоу для умной лампочки Яндекса — чистая математика без DOM и сети.
 *
 * Рисунок — клетки на сетке долей: ключ = индекс четверти доли, значение =
 * цвет { h, s, b, sharp }. Пустая клетка значит «держать прошлый цвет».
 *
 * Лампа проигрывает свою анимацию из шагов { h, s, b, d }: d — время, за
 * которое она ПЕРЕТЕКАЕТ в цвет шага; анимация крутится по кругу. Проверено
 * на лампе: больше 16 шагов она молча не принимает, а команда идёт до неё
 * ~0,3–0,6 с. Поэтому шоу уходит кусками по ≤16 шагов, каждый — заранее.
 *
 * Время берём из настоящих долей трека (beats), а не из BPM: у части треков
 * танцпола темп плывёт или меняется посередине.
 */

export const MAX_STEPS = 16;
export const SHARP_MS = 80;        // за столько «резкая» клетка входит в цвет
export const PAD_MS = 3000;        // запасной шаг в конце куска, если следующий опоздает
export const MAX_STEP_MS = 590000; // посредник режет шаг до 600 с
export const DARK = { h: 0, s: 0, b: 1, sharp: true };

export const PALETTE = [
	{ name: 'Красный', h: 0, s: 100 }, { name: 'Оранжевый', h: 28, s: 100 },
	{ name: 'Жёлтый', h: 52, s: 100 }, { name: 'Зелёный', h: 120, s: 100 },
	{ name: 'Голубой', h: 185, s: 100 }, { name: 'Синий', h: 235, s: 100 },
	{ name: 'Розовый', h: 320, s: 100 }, { name: 'Белый', h: 0, s: 0 },
	{ name: 'Тьма', h: 0, s: 0, b: 1 },
];

/** Доли из BPM и первой доли — для своего файла без разбора. */
export function gridFromBpm(bpm, first, duration) {
	bpm = Math.min(240, Math.max(40, +bpm || 120));       // защита от зависания на кривом вводе
	const step = 60 / bpm, out = [];
	for (let t = first; t <= duration + step; t += step) out.push(+t.toFixed(4));
	return out;
}

/** Время дробной доли. За краями — продолжаем крайним интервалом. */
export function beatTime(beats, bf) {
	const n = beats.length;
	if (n < 2) return (beats[0] || 0) + bf * 0.5;
	if (bf <= 0) return beats[0] + bf * (beats[1] - beats[0]);
	if (bf >= n - 1) return beats[n - 1] + (bf - (n - 1)) * (beats[n - 1] - beats[n - 2]);
	const i = Math.floor(bf);
	return beats[i] + (bf - i) * (beats[i + 1] - beats[i]);
}

/** Обратное к beatTime: дробная доля по времени. */
export function beatAt(beats, t) {
	const n = beats.length;
	if (n < 2) return (t - (beats[0] || 0)) / 0.5;
	if (t <= beats[0]) return (t - beats[0]) / (beats[1] - beats[0]);
	if (t >= beats[n - 1]) return n - 1 + (t - beats[n - 1]) / (beats[n - 1] - beats[n - 2]);
	let lo = 0, hi = n - 1;
	while (hi - lo > 1) { const m = (lo + hi) >> 1; if (beats[m] <= t) lo = m; else hi = m; }
	return lo + (t - beats[lo]) / (beats[lo + 1] - beats[lo]);
}

export const qTime = (beats, q) => beatTime(beats, q / 4);

export function sameColor(a, b) {
	return !!a && !!b && a.h === b.h && a.s === b.s && (a.b ?? 100) === (b.b ?? 100) && !!a.sharp === !!b.sharp;
}

/** Смены цвета начиная с fromT. Цвет, действующий в fromT, идёт первым событием. */
export function events(cells, beats, fromT) {
	const keys = Object.keys(cells).map(Number).sort((a, b) => a - b);
	const out = [];
	let prev = null;
	for (const q of keys) {
		const t = qTime(beats, q), c = cells[q];
		if (t < fromT - 1e-6) { prev = c; continue; }
		if (!out.length && prev && t > fromT + 0.05) out.push({ t: fromT, q: null, c: prev });
		if (out.length && sameColor(out[out.length - 1].c, c)) continue;
		out.push({ t, q, c });
	}
	if (!out.length && prev) out.push({ t: fromT, q: null, c: prev });
	return out;
}

/** Шаги одной смены цвета длиной ms. cellMs — длина клетки: плавный переход длится одну клетку. */
export function stepsFor(c, ms, cellMs) {
	const st = (d) => ({ h: c.h, s: c.s, b: c.b ?? 100, d: Math.min(MAX_STEP_MS, Math.max(50, Math.round(d))) });
	if (c.sharp) return ms > SHARP_MS * 2 ? [st(SHARP_MS), st(ms - SHARP_MS)] : [st(ms)];
	return cellMs && ms > cellMs + 50 ? [st(cellMs), st(ms - cellMs)] : [st(ms)];
}

/**
 * Куски для лампы: { t, steps } — t = когда кусок должен заиграть.
 * В куске ≤ MAX_STEPS-1 шагов плюс запасной шаг: тот же цвет, что последний.
 */
export function buildChunks(cells, beats, fromT, { cellQ = 2, endT = null, darkGap = false } = {}) {
	const ev = events(cells, beats, fromT), out = [];
	// до первой клетки лампа должна стоять тёмной, а не крутить прошлый кусок
	if (darkGap && (!ev.length || ev[0].t > fromT + 0.05)) ev.unshift({ t: fromT, q: null, c: DARK });
	let cur = null;
	ev.forEach((e, i) => {
		// последний цвет держим до конца трека: иначе лампа пойдёт по кругу последнего куска
		const end = i + 1 < ev.length ? ev[i + 1].t : Math.max(e.t + 4, endT ?? 0);
		const q = e.q ?? Math.floor(beatAt(beats, e.t) * 4);
		const cellMs = (qTime(beats, q + cellQ) - qTime(beats, q)) * 1000;
		const st = stepsFor(e.c, (end - e.t) * 1000, cellMs);
		if (!cur || cur.steps.length + st.length > MAX_STEPS - 1) { cur = { t: e.t, steps: [] }; out.push(cur); }
		cur.steps.push(...st);
	});
	for (const ch of out) { const last = ch.steps[ch.steps.length - 1]; ch.steps.push({ ...last, d: PAD_MS }); }
	return out;
}

/** Цвет, который должен гореть в момент t (для превью и «заморозки» на паузе). */
export function colorAt(cells, beats, t) {
	const keys = Object.keys(cells).map(Number).sort((a, b) => a - b);
	let c = null;
	for (const q of keys) { if (qTime(beats, q) <= t + 1e-6) c = cells[q]; else break; }
	return c;
}

/* Пары соседних оттенков: быстрое чередование далёких цветов лампа смешивает
   (красный↔синий даёт розово-фиолетовую кашу), а соседние читаются чисто. */
const FAMILIES = [[0, 28], [185, 220], [320, 345], [120, 160], [40, 55]];

/**
 * Автосвет по разбору трека танцпола: bars — уровень такта (l/m/h),
 * events — такты смены части. Тихо — медленный перелив раз в такт,
 * средне — цвет на каждую долю, мощно — вспышка в долю и тьма между,
 * смена части — белая вспышка и новая пара цветов.
 */
export function autoShow(track, { beatsPerBar = 4 } = {}) {
	const cells = {}, bars = track.bars || '', marks = new Set(track.events || []);
	let fam = 0;
	for (let bar = 0; bar < bars.length; bar++) {
		if (marks.has(bar) && bar > 0) fam = (fam + 1) % FAMILIES.length;
		const [a, b] = FAMILIES[fam], lvl = bars[bar], q0 = bar * beatsPerBar * 4;
		if (lvl === 'l') {
			cells[q0] = { h: bar % 2 ? b : a, s: 100, b: 55, sharp: false };
		} else if (lvl === 'm') {
			for (let k = 0; k < beatsPerBar; k++) cells[q0 + k * 4] = { h: k % 2 ? b : a, s: 100, b: 100, sharp: true };
		} else {
			for (let k = 0; k < beatsPerBar; k++) {
				cells[q0 + k * 4] = { h: k % 2 ? b : a, s: 100, b: 100, sharp: true };
				cells[q0 + k * 4 + 2] = { h: 0, s: 0, b: 1, sharp: true };
			}
		}
		if (marks.has(bar)) cells[q0] = { h: 0, s: 0, b: 100, sharp: true };
	}
	return cells;
}
