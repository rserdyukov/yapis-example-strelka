// Хост для модулей, сгенерированных fsmc. Работает и в Node, и в браузере:
// зависимостей нет, только стандартный WebAssembly API.
//
// Модуль сам ведёт автомат (или систему автоматов); хост лишь
//   * даёт функции вывода (env.print_str / print_int / print_end),
//   * кладёт строковые аргументы событий в линейную память через alloc,
//   * превращает ошибки выполнения (env.fail) в исключения JS,
//   * читает описание машины из пользовательской секции fsm.meta.
//
//   const m = await FsmMachine.load(bytes, { onLine: console.log });
//   m.send("pin", "4321");     // → { status: "moved", from: "PinWait", to: "Menu", output: [...] }
//   m.send("you.call");        // в системе событие адресуется экземпляру

const STATUS = ["moved", "ignored", "unhandled"];
const utf8 = new TextEncoder();
const text = new TextDecoder();

export class FsmMachine {
  static async load(bytes, { onLine = () => {} } = {}) {
    const module = await WebAssembly.compile(bytes);
    const sections = WebAssembly.Module.customSections(module, "fsm.meta");
    if (!sections.length) throw new Error("в модуле нет секции fsm.meta — он собран не fsmc?");
    const meta = JSON.parse(text.decode(sections[0]));
    const machine = new FsmMachine(meta, onLine);
    const instance = await WebAssembly.instantiate(module, { env: machine.#imports() });
    machine.#bind(instance);
    machine.reset();
    return machine;
  }

  #exports;
  #line = "";
  #output = [];
  #dropped = [];

  constructor(meta, onLine) {
    this.meta = meta;
    this.onLine = onLine;
  }

  #imports() {
    return {
      print_str: (ptr) => { this.#line += this.#readString(ptr); },
      print_int: (n) => { this.#line += String(n); },
      print_end: () => {
        this.#output.push(this.#line);
        this.onLine(this.#line);
        this.#line = "";
      },
      fail: (ptr, value) => {
        throw new FsmError(`ошибка выполнения: ${this.#readString(ptr)} (${value})`);
      },
      dropped: (pair) => { this.#dropped.push(this.meta.pairs[pair]); },
    };
  }

  #bind(instance) {
    this.#exports = instance.exports;
  }

  #memory() {
    // buffer пересоздаётся после memory.grow — берём его каждый раз заново.
    return this.#exports.memory.buffer;
  }

  #readString(ptr) {
    const len = new DataView(this.#memory()).getUint32(ptr, true);
    return text.decode(new Uint8Array(this.#memory(), ptr + 4, len));
  }

  #writeString(s) {
    const data = utf8.encode(s);
    const ptr = this.#exports.alloc(4 + data.length);
    new DataView(this.#memory()).setUint32(ptr, data.length, true);
    new Uint8Array(this.#memory(), ptr + 4, data.length).set(data);
    return ptr;
  }

  #value(type, raw) {
    return type === "string" ? this.#readString(raw) : type === "bool" ? Boolean(raw) : raw;
  }

  #field(f) {
    const array = /^(\w+)\[(\d+)\]$/.exec(f.type);
    if (!array) return this.#value(f.type, this.#exports[f.export].value);
    const view = new DataView(this.#memory());
    return Array.from({ length: Number(array[2]) },
      (_, i) => this.#value(array[1], view.getInt32(f.addr + 4 * i, true)));
  }

  reset() {
    this.#exports.init();
    this.#line = "";
  }

  get isSystem() {
    return this.meta.kind === "system";
  }

  // Состояния экземпляров: { table: "Deal", you: "Wait" }; у machine ключ "".
  get states() {
    const out = {};
    for (const inst of this.meta.instances) {
      out[inst.name] = inst.states[this.#exports[inst.state].value];
    }
    return out;
  }

  // Состояние одной строкой: "Menu" или "table=Deal, you=Wait".
  get state() {
    const s = this.states;
    if (!this.isSystem) return s[""];
    return Object.entries(s).map(([k, v]) => `${k}=${v}`).join(", ");
  }

  // Контекст: у machine — { поле: значение }, у системы — { экземпляр: { ... } }.
  get context() {
    const out = {};
    for (const inst of this.meta.instances) {
      const ctx = {};
      for (const f of inst.context) ctx[f.name] = this.#field(f);
      if (!this.isSystem) return ctx;
      out[inst.name] = ctx;
    }
    return out;
  }

  get events() {
    return this.meta.events;
  }

  // Отправить событие. args — значения параметров в порядке объявления;
  // строки передаются как есть, числа — как number, bool — как true/false.
  send(name, ...args) {
    const event = this.meta.events.find((e) => e.name === name);
    if (!event) throw new Error(`неизвестное событие '${name}'`);
    if (args.length !== event.params.length) {
      throw new Error(`${name} ожидает ${event.params.length} аргумент(а), передано ${args.length}`);
    }
    const raw = event.params.map((p, i) => {
      const v = args[i];
      if (p.type === "string") return this.#writeString(String(v));
      if (p.type === "bool") return v === true || v === "true" ? 1 : 0;
      const n = Number(v);
      if (!Number.isInteger(n)) throw new Error(`${name}: параметр ${p.name} ожидает целое число, получено '${v}'`);
      return n | 0;
    });
    const from = this.state;
    this.#output = [];
    this.#dropped = [];
    const status = STATUS[this.#exports[event.export](...raw)];
    return { event: name, args, status, from, to: this.state, output: this.#output,
             dropped: this.#dropped };
  }
}

export class FsmError extends Error {}

// Сценарий: строка на событие, `событие арг1 арг2 ...`, `#` — комментарий.
// Аргумент с пробелами берётся в двойные кавычки: `say "добрый день"`.
export function parseScenario(source) {
  const events = [];
  source.split("\n").forEach((raw, i) => {
    const words = [...raw.matchAll(/"([^"]*)"|(#.*)|(\S+)/g)]
      .filter((m) => !m[2])
      .map((m) => (m[1] !== undefined ? m[1] : m[3]));
    if (!words.length) return;
    events.push({ name: words[0], args: words.slice(1), line: i + 1 });
  });
  return events;
}
