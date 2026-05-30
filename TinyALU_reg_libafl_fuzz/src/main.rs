use std::{
    collections::BTreeSet,
    env,
    error::Error,
    fs::{self, File},
    io::Write,
    path::{Path, PathBuf},
    ptr::write,
};

use libafl::{
    corpus::{Corpus, InMemoryCorpus, OnDiskCorpus},
    events::SimpleEventManager,
    executors::{ExitKind, inprocess::InProcessExecutor},
    feedbacks::{CrashFeedback, MaxMapFeedback},
    fuzzer::{Fuzzer, StdFuzzer},
    inputs::{BytesInput, HasTargetBytes},
    monitors::NopMonitor,
    mutators::{havoc_mutations::havoc_mutations, scheduled::HavocScheduledMutator},
    observers::ConstMapObserver,
    schedulers::QueueScheduler,
    stages::mutational::StdMutationalStage,
    state::{HasCorpus, StdState},
};
use libafl_bolts::{AsSlice, current_nanos, nonnull_raw_mut, rands::StdRand, tuples::tuple_list};
use serde::Serialize;
use serde_json::Value;

const OPS: [u8; 4] = [1, 2, 3, 4];
const BYTE_EDGES: [u8; 8] = [0x00, 0x01, 0x02, 0x03, 0x7f, 0x80, 0xfe, 0xff];
const SIGNALS_LEN: usize = 32;

static mut SIGNALS: [u8; SIGNALS_LEN] = [0; SIGNALS_LEN];
static mut SIGNALS_PTR: *mut u8 = &raw mut SIGNALS as _;

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct TinyAluCase {
    a: u8,
    b: u8,
    op: u8,
}

#[derive(Clone, Debug)]
struct ExportCase {
    case: TinyAluCase,
    origin: String,
}

#[derive(Serialize)]
struct JsonCase<'a> {
    a: u8,
    b: u8,
    op: u8,
    origin: &'a str,
}

#[derive(Debug)]
struct Config {
    corpus_out: PathBuf,
    crashes_dir: PathBuf,
    directives_path: Option<PathBuf>,
    iters: u64,
    max_seed_cases: usize,
    seed: u64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            corpus_out: PathBuf::from("coverage/libafl_fuzz_corpus.jsonl"),
            crashes_dir: PathBuf::from("crashes"),
            directives_path: None,
            iters: env_u64("LIBAFL_ITERS", 256),
            max_seed_cases: env_usize("LIBAFL_MAX_SEEDS", 32),
            seed: env_u64("LIBAFL_SEED", 1),
        }
    }
}

impl Config {
    fn from_args() -> Result<Option<Self>, Box<dyn Error>> {
        let mut config = Self::default();
        let mut args = env::args().skip(1);

        while let Some(arg) = args.next() {
            match arg.as_str() {
                "-h" | "--help" => {
                    print_help();
                    return Ok(None);
                }
                "--corpus-out" => {
                    config.corpus_out = PathBuf::from(require_value(&mut args, "--corpus-out")?);
                }
                "--crashes-dir" => {
                    config.crashes_dir = PathBuf::from(require_value(&mut args, "--crashes-dir")?);
                }
                "--directives" => {
                    config.directives_path =
                        Some(PathBuf::from(require_value(&mut args, "--directives")?));
                }
                "--iters" => {
                    config.iters = require_value(&mut args, "--iters")?.parse()?;
                }
                "--max-seeds" => {
                    config.max_seed_cases = require_value(&mut args, "--max-seeds")?.parse()?;
                }
                "--seed" => {
                    config.seed = parse_u64(&require_value(&mut args, "--seed")?)?;
                }
                other => {
                    return Err(format!("unknown argument: {other}").into());
                }
            }
        }

        if let Ok(path) = env::var("LIBAFL_DIRECTIVES")
            && config.directives_path.is_none()
        {
            config.directives_path = Some(PathBuf::from(path));
        }
        if let Ok(path) = env::var("LIBAFL_CORPUS_OUT") {
            config.corpus_out = PathBuf::from(path);
        }

        Ok(Some(config))
    }
}

fn main() {
    match run() {
        Ok(()) => {}
        Err(err) => {
            eprintln!("tinyalu_reg_libafl_fuzz: {err}");
            std::process::exit(1);
        }
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let Some(config) = Config::from_args()? else {
        return Ok(());
    };

    let mandatory = mandatory_cases();
    let directed = match &config.directives_path {
        Some(path) => load_directive_cases(path)?,
        None => Vec::new(),
    };
    let random_seeds = pseudo_random_seed_cases(config.seed, config.max_seed_cases);

    let seed_inputs: Vec<BytesInput> = mandatory
        .iter()
        .chain(directed.iter())
        .chain(random_seeds.iter())
        .map(|case| BytesInput::new(case_to_input(case.case)))
        .collect();
    let seed_count = seed_inputs.len();

    let observer = unsafe { ConstMapObserver::from_mut_ptr("signals", nonnull_raw_mut!(SIGNALS)) };
    let mut feedback = MaxMapFeedback::new(&observer);
    let mut objective = CrashFeedback::new();

    let mut state = StdState::new(
        StdRand::with_seed(config.seed ^ current_nanos()),
        InMemoryCorpus::new(),
        OnDiskCorpus::new(config.crashes_dir.clone())?,
        &mut feedback,
        &mut objective,
    )?;

    let monitor = NopMonitor::new();
    let mut mgr = SimpleEventManager::new(monitor);
    let scheduler = QueueScheduler::new();
    let mut fuzzer = StdFuzzer::new(scheduler, feedback, objective);

    let mut harness = |input: &BytesInput| {
        let bytes = input.target_bytes();
        observe_input(bytes.as_slice())
    };
    let mut executor = InProcessExecutor::new(
        &mut harness,
        tuple_list!(observer),
        &mut fuzzer,
        &mut state,
        &mut mgr,
    )?;

    let mut generator = seed_inputs.into_iter();
    state.generate_initial_inputs_forced(
        &mut fuzzer,
        &mut executor,
        &mut generator,
        &mut mgr,
        seed_count,
    )?;

    if config.iters > 0 && !state.corpus().is_empty() {
        let mutator = HavocScheduledMutator::new(havoc_mutations());
        let mut stages = tuple_list!(StdMutationalStage::new(mutator));
        fuzzer.fuzz_loop_for(
            &mut stages,
            &mut executor,
            &mut state,
            &mut mgr,
            config.iters,
        )?;
    }

    let mut export_cases = Vec::new();
    export_cases.extend(mandatory);
    export_cases.extend(directed);
    export_cases.extend(random_seeds);

    for id in state.corpus().ids() {
        let input = state.corpus().cloned_input_for_id(id)?;
        if let Some(case) = TinyAluCase::decode(input.as_ref()) {
            export_cases.push(ExportCase {
                case,
                origin: "libafl_corpus".to_string(),
            });
        }
    }

    let written = write_jsonl(&config.corpus_out, &dedupe_cases(export_cases))?;
    println!(
        "LibAFL TinyALU corpus: wrote {written} cases to {}",
        config.corpus_out.display()
    );
    Ok(())
}

fn print_help() {
    println!(
        "\
TinyALU_reg LibAFL corpus generator

Options:
  --corpus-out PATH   JSONL corpus to write [default: coverage/libafl_fuzz_corpus.jsonl]
  --crashes-dir PATH  LibAFL objective corpus directory [default: crashes]
  --directives PATH   coverage_feedback.py mutation directives JSON
  --iters N           LibAFL fuzz iterations [default: LIBAFL_ITERS or 256]
  --max-seeds N       deterministic random seed cases [default: LIBAFL_MAX_SEEDS or 32]
  --seed N            deterministic seed [default: LIBAFL_SEED or 1]
"
    );
}

fn require_value(
    args: &mut impl Iterator<Item = String>,
    flag: &str,
) -> Result<String, Box<dyn Error>> {
    args.next()
        .ok_or_else(|| format!("{flag} requires a value").into())
}

fn env_u64(name: &str, default: u64) -> u64 {
    env::var(name)
        .ok()
        .and_then(|value| parse_u64(&value).ok())
        .unwrap_or(default)
}

fn env_usize(name: &str, default: usize) -> usize {
    env::var(name)
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(default)
}

fn parse_u64(text: &str) -> Result<u64, Box<dyn Error>> {
    let text = text.trim();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        Ok(u64::from_str_radix(hex, 16)?)
    } else {
        Ok(text.parse()?)
    }
}

impl TinyAluCase {
    fn decode(bytes: &[u8]) -> Option<Self> {
        if bytes.len() < 3 {
            return None;
        }
        Some(Self {
            a: bytes[0],
            b: bytes[1],
            op: OPS[usize::from(bytes[2] % OPS.len() as u8)],
        })
    }
}

fn case_to_input(case: TinyAluCase) -> Vec<u8> {
    let op_selector = OPS.iter().position(|op| *op == case.op).unwrap_or(0) as u8;
    vec![case.a, case.b, op_selector]
}

fn observe_input(bytes: &[u8]) -> ExitKind {
    signals_clear();
    signals_set(0);

    let Some(case) = TinyAluCase::decode(bytes) else {
        signals_set(31);
        return ExitKind::Ok;
    };

    signals_set(usize::from(case.op));
    if case.a == 0 {
        signals_set(5);
    }
    if case.b == 0 {
        signals_set(6);
    }
    if case.a == 0xff {
        signals_set(7);
    }
    if case.b == 0xff {
        signals_set(8);
    }
    if matches!(case.a, 0x7f | 0x80) {
        signals_set(9);
    }
    if matches!(case.b, 0x7f | 0x80) {
        signals_set(10);
    }
    if case.op == 1 && u16::from(case.a) + u16::from(case.b) >= 0x100 {
        signals_set(11);
    }
    if case.op == 4 && case.a != 0 && case.b != 0 {
        signals_set(12);
    }
    if is_dense_bit_pattern(case.a, case.b) {
        signals_set(13);
    }
    if case.a == case.b {
        signals_set(14);
    }
    if case.a ^ case.b == 0xff {
        signals_set(15);
    }
    if alu_prediction(case) == 0 {
        signals_set(16);
    }

    ExitKind::Ok
}

fn signals_clear() {
    for idx in 0..SIGNALS_LEN {
        unsafe { write(SIGNALS_PTR.add(idx), 0) };
    }
}

fn signals_set(idx: usize) {
    if idx < SIGNALS_LEN {
        unsafe { write(SIGNALS_PTR.add(idx), 1) };
    }
}

fn alu_prediction(case: TinyAluCase) -> u16 {
    match case.op {
        1 => u16::from(case.a) + u16::from(case.b),
        2 => u16::from(case.a & case.b),
        3 => u16::from(case.a ^ case.b),
        4 => u16::from(case.a) * u16::from(case.b),
        _ => unreachable!("TinyAluCase always stores a legal op"),
    }
}

fn is_dense_bit_pattern(a: u8, b: u8) -> bool {
    matches!(
        (a, b),
        (0x55, 0xaa) | (0xaa, 0x55) | (0x0f, 0xf0) | (0xf0, 0x0f)
    )
}

fn mandatory_cases() -> Vec<ExportCase> {
    let mut cases = Vec::new();
    for op in OPS {
        cases.push(export_case(0x00, 0x00, op, "mandatory_op"));
        cases.push(export_case(0xff, 0xff, op, "mandatory_max"));
    }

    let directed_pairs = [
        (0x00, 0xff),
        (0xff, 0x00),
        (0x7f, 0x80),
        (0x55, 0xaa),
        (0xaa, 0x55),
    ];
    for op in OPS {
        for (a, b) in directed_pairs {
            cases.push(export_case(a, b, op, "directed_pair"));
        }
    }
    cases
}

fn pseudo_random_seed_cases(seed: u64, count: usize) -> Vec<ExportCase> {
    let mut rng = Lcg::new(seed);
    let mut cases = Vec::with_capacity(count);
    for idx in 0..count {
        let a = biased_byte(&mut rng, idx);
        let b = biased_byte(&mut rng, idx + 3);
        let op = OPS[(rng.next_u64() as usize) % OPS.len()];
        cases.push(export_case(a, b, op, "libafl_seed"));
    }
    cases
}

fn biased_byte(rng: &mut Lcg, idx: usize) -> u8 {
    if idx % 3 == 0 {
        BYTE_EDGES[(rng.next_u64() as usize) % BYTE_EDGES.len()]
    } else {
        rng.next_u8()
    }
}

#[derive(Debug)]
struct Lcg {
    state: u64,
}

impl Lcg {
    fn new(seed: u64) -> Self {
        Self {
            state: seed ^ 0x9e37_79b9_7f4a_7c15,
        }
    }

    fn next_u64(&mut self) -> u64 {
        self.state = self
            .state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.state
    }

    fn next_u8(&mut self) -> u8 {
        (self.next_u64() >> 32) as u8
    }
}

fn load_directive_cases(path: &Path) -> Result<Vec<ExportCase>, Box<dyn Error>> {
    let text = fs::read_to_string(path)?;
    let value: Value = serde_json::from_str(&text)?;
    Ok(directive_cases_from_value(&value))
}

fn directive_cases_from_value(value: &Value) -> Vec<ExportCase> {
    let directives = if let Some(items) = value.as_array() {
        items.as_slice()
    } else if let Some(items) = value.get("directives").and_then(Value::as_array) {
        items.as_slice()
    } else {
        &[]
    };

    let mut cases = Vec::new();
    for (idx, directive) in directives.iter().enumerate() {
        let origin = directive
            .get("name")
            .and_then(Value::as_str)
            .map(str::to_string)
            .unwrap_or_else(|| format!("directive_{idx}"));
        let ops = resolve_ops(directive);
        let mut pairs = Vec::new();

        if let Some(operand_pairs) = directive.get("operand_pairs").and_then(Value::as_array) {
            for pair in operand_pairs {
                if let (Some(a), Some(b)) = (
                    pair.get("a").and_then(parse_u8_value),
                    pair.get("b").and_then(parse_u8_value),
                ) {
                    pairs.push((a, b));
                }
            }
        }

        if let Some(operands) = directive.get("operands").and_then(Value::as_object)
            && let (Some(a_values), Some(b_values)) = (
                operands.get("A").and_then(Value::as_array),
                operands.get("B").and_then(Value::as_array),
            )
        {
            for a in a_values.iter().filter_map(parse_u8_value) {
                for b in b_values.iter().filter_map(parse_u8_value) {
                    pairs.push((a, b));
                }
            }
        }

        pairs.extend(constraint_pairs(directive));
        if pairs.is_empty() {
            pairs.extend([(0x00, 0x00), (0xff, 0xff), (0x55, 0xaa), (0x7f, 0x80)]);
        }

        for op in ops {
            for (a, b) in &pairs {
                cases.push(export_case(*a, *b, op, &origin));
            }
        }
    }
    cases
}

fn resolve_ops(directive: &Value) -> Vec<u8> {
    let raw_ops = directive
        .get("ops")
        .or_else(|| directive.get("op_bias"))
        .or_else(|| directive.get("op"));

    let Some(raw_ops) = raw_ops else {
        return OPS.to_vec();
    };

    let mut resolved = Vec::new();
    match raw_ops {
        Value::Array(items) => {
            for item in items {
                if let Some(op) = parse_op_value(item)
                    && !resolved.contains(&op)
                {
                    resolved.push(op);
                }
            }
        }
        item => {
            if let Some(op) = parse_op_value(item) {
                resolved.push(op);
            }
        }
    }

    if resolved.is_empty() {
        OPS.to_vec()
    } else {
        resolved
    }
}

fn parse_op_value(value: &Value) -> Option<u8> {
    if let Some(name) = value.as_str() {
        if !name.starts_with("0x") && !name.starts_with("0X") {
            return match name.to_ascii_uppercase().as_str() {
                "ADD" => Some(1),
                "AND" => Some(2),
                "XOR" => Some(3),
                "MUL" => Some(4),
                _ => None,
            };
        }
    }

    parse_u8_value(value).filter(|op| OPS.contains(op))
}

fn parse_u8_value(value: &Value) -> Option<u8> {
    match value {
        Value::Number(number) => number.as_u64().map(|number| number as u8),
        Value::String(text) => {
            let text = text.trim();
            let parsed =
                if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
                    u16::from_str_radix(hex, 16).ok()
                } else {
                    text.parse::<u16>().ok()
                }?;
            Some((parsed & 0xff) as u8)
        }
        _ => None,
    }
}

fn constraint_pairs(directive: &Value) -> Vec<(u8, u8)> {
    let constraints: Vec<String> = directive
        .get("constraints")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(str::to_ascii_uppercase)
        .collect();

    let has = |names: &[&str]| {
        constraints
            .iter()
            .any(|item| names.contains(&item.as_str()))
    };
    let mut pairs = Vec::new();
    if has(&["ADD_OVERFLOW", "A_PLUS_B_GE_256"]) {
        pairs.extend([(0xff, 0xff), (0xff, 0x01), (0x80, 0x80), (0xfe, 0x02)]);
    }
    if has(&["MUL_NONZERO", "MUL_PIPELINE", "THREE_CYCLE"]) {
        pairs.extend([(0x01, 0xff), (0x7f, 0x80), (0x80, 0x80), (0xff, 0xfe)]);
    }
    if has(&["BIT_PATTERN", "TOGGLE_DENSE"]) {
        pairs.extend([(0x55, 0xaa), (0xaa, 0x55), (0x0f, 0xf0), (0xf0, 0x0f)]);
    }
    pairs
}

fn export_case(a: u8, b: u8, op: u8, origin: &str) -> ExportCase {
    ExportCase {
        case: TinyAluCase { a, b, op },
        origin: origin.to_string(),
    }
}

fn dedupe_cases(cases: Vec<ExportCase>) -> Vec<ExportCase> {
    let mut seen = BTreeSet::new();
    let mut unique = Vec::new();
    for case in cases {
        if seen.insert(case.case) {
            unique.push(case);
        }
    }
    unique
}

fn write_jsonl(path: &Path, cases: &[ExportCase]) -> Result<usize, Box<dyn Error>> {
    if let Some(parent) = path.parent()
        && !parent.as_os_str().is_empty()
    {
        fs::create_dir_all(parent)?;
    }

    let mut file = File::create(path)?;
    for case in cases {
        serde_json::to_writer(
            &mut file,
            &JsonCase {
                a: case.case.a,
                b: case.case.b,
                op: case.case.op,
                origin: &case.origin,
            },
        )?;
        writeln!(file)?;
    }
    Ok(cases.len())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn decode_maps_selector_to_legal_ops() {
        assert_eq!(TinyAluCase::decode(&[1, 2, 0]).unwrap().op, 1);
        assert_eq!(TinyAluCase::decode(&[1, 2, 1]).unwrap().op, 2);
        assert_eq!(TinyAluCase::decode(&[1, 2, 2]).unwrap().op, 3);
        assert_eq!(TinyAluCase::decode(&[1, 2, 3]).unwrap().op, 4);
        assert_eq!(TinyAluCase::decode(&[1, 2, 7]).unwrap().op, 4);
        assert!(TinyAluCase::decode(&[1, 2]).is_none());
    }

    #[test]
    fn mandatory_cases_cover_all_ops_and_edges() {
        let cases = mandatory_cases();
        let ops: BTreeSet<u8> = cases.iter().map(|case| case.case.op).collect();
        assert_eq!(ops, BTreeSet::from(OPS));
        for op in OPS {
            assert!(
                cases
                    .iter()
                    .any(|case| case.case == TinyAluCase { a: 0, b: 0, op })
            );
            assert!(cases.iter().any(|case| case.case
                == TinyAluCase {
                    a: 0xff,
                    b: 0xff,
                    op
                }));
        }
    }

    #[test]
    fn directives_accept_names_constraints_and_operand_grids() {
        let value = json!({
            "directives": [
                {
                    "name": "stress_add",
                    "ops": ["ADD"],
                    "constraints": ["ADD_OVERFLOW"]
                },
                {
                    "name": "grid",
                    "ops": ["AND", "XOR"],
                    "operands": {
                        "A": ["0x00", "0xff"],
                        "B": ["0x55"]
                    }
                }
            ]
        });

        let cases = directive_cases_from_value(&value);
        assert!(cases.iter().any(|case| case.case
            == TinyAluCase {
                a: 0xff,
                b: 0x01,
                op: 1
            }));
        assert!(cases.iter().any(|case| case.case
            == TinyAluCase {
                a: 0xff,
                b: 0x55,
                op: 2
            }));
        assert!(cases.iter().any(|case| case.case
            == TinyAluCase {
                a: 0x00,
                b: 0x55,
                op: 3
            }));
    }
}
