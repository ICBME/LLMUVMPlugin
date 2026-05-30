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
use serde_json::{Value, json};

const TINYALU_OPS: [u8; 4] = [1, 2, 3, 4];
const BYTE_EDGES: [u8; 8] = [0x00, 0x01, 0x02, 0x03, 0x7f, 0x80, 0xfe, 0xff];
const SIGNALS_LEN: usize = 64;

static mut SIGNALS: [u8; SIGNALS_LEN] = [0; SIGNALS_LEN];
static mut SIGNALS_PTR: *mut u8 = &raw mut SIGNALS as _;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Target {
    TinyAlu,
    Aes,
    Sha256,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum FuzzCase {
    TinyAlu(TinyAluCase),
    Aes(AesCase),
    Sha256(Sha256Case),
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct TinyAluCase {
    a: u8,
    b: u8,
    op: u8,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct AesCase {
    key_len: u16,
    encdec: AesDirection,
    key: Vec<u8>,
    block: [u8; 16],
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum AesDirection {
    Encipher,
    Decipher,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct Sha256Case {
    mode: ShaMode,
    message: Vec<u8>,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum ShaMode {
    Sha224,
    Sha256,
}

#[derive(Clone, Debug)]
struct ExportCase {
    case: FuzzCase,
    origin: String,
}

#[derive(Debug)]
struct Config {
    target: Target,
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
            target: Target::TinyAlu,
            corpus_out: PathBuf::from("coverage/tinyalu_corpus.jsonl"),
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
        let mut corpus_overridden = false;
        let mut args = env::args().skip(1);

        while let Some(arg) = args.next() {
            match arg.as_str() {
                "-h" | "--help" => {
                    print_help();
                    return Ok(None);
                }
                "--target" => {
                    config.target = Target::parse(&require_value(&mut args, "--target")?)?;
                }
                "--corpus-out" => {
                    config.corpus_out = PathBuf::from(require_value(&mut args, "--corpus-out")?);
                    corpus_overridden = true;
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
                other => return Err(format!("unknown argument: {other}").into()),
            }
        }

        if let Ok(target) = env::var("FUZZ_TARGET") {
            config.target = Target::parse(&target)?;
        }
        if let Ok(path) = env::var("LIBAFL_DIRECTIVES")
            && config.directives_path.is_none()
        {
            config.directives_path = Some(PathBuf::from(path));
        }
        if let Ok(path) = env::var("LIBAFL_CORPUS_OUT") {
            config.corpus_out = PathBuf::from(path);
            corpus_overridden = true;
        }
        if !corpus_overridden {
            config.corpus_out =
                PathBuf::from(format!("coverage/{}_corpus.jsonl", config.target.name()));
        }

        Ok(Some(config))
    }
}

fn main() {
    if let Err(err) = run() {
        eprintln!("libafl_bfm_fuzz: {err}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let Some(config) = Config::from_args()? else {
        return Ok(());
    };

    let mandatory = config.target.mandatory_cases();
    let directed = match (&config.target, &config.directives_path) {
        (Target::TinyAlu, Some(path)) => load_tinyalu_directive_cases(path)?,
        (_, Some(path)) => {
            eprintln!(
                "warning: directives are currently TinyALU-specific; ignoring {} for {}",
                path.display(),
                config.target.name()
            );
            Vec::new()
        }
        (_, None) => Vec::new(),
    };
    let random_seeds = config
        .target
        .pseudo_random_seed_cases(config.seed, config.max_seed_cases);

    let seed_inputs: Vec<BytesInput> = mandatory
        .iter()
        .chain(directed.iter())
        .chain(random_seeds.iter())
        .map(|case| BytesInput::new(config.target.case_to_input(&case.case)))
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

    let target = config.target;
    let mut harness = |input: &BytesInput| {
        let bytes = input.target_bytes();
        target.observe_input(bytes.as_slice())
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
        if let Some(case) = config.target.decode_case(input.as_ref()) {
            export_cases.push(ExportCase {
                case,
                origin: "libafl_corpus".to_string(),
            });
        }
    }

    let written = write_jsonl(&config.corpus_out, &dedupe_cases(export_cases))?;
    println!(
        "LibAFL {} corpus: wrote {written} cases to {}",
        config.target.name(),
        config.corpus_out.display()
    );
    Ok(())
}

impl Target {
    fn parse(text: &str) -> Result<Self, Box<dyn Error>> {
        match text.trim().to_ascii_lowercase().as_str() {
            "tinyalu" | "tinyalu_reg" => Ok(Self::TinyAlu),
            "aes" => Ok(Self::Aes),
            "sha256" | "sha224" | "sha2" => Ok(Self::Sha256),
            other => Err(format!("unknown fuzz target: {other}").into()),
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::TinyAlu => "tinyalu",
            Self::Aes => "aes",
            Self::Sha256 => "sha256",
        }
    }

    fn decode_case(self, bytes: &[u8]) -> Option<FuzzCase> {
        match self {
            Self::TinyAlu => TinyAluCase::decode(bytes).map(FuzzCase::TinyAlu),
            Self::Aes => AesCase::decode(bytes).map(FuzzCase::Aes),
            Self::Sha256 => Sha256Case::decode(bytes).map(FuzzCase::Sha256),
        }
    }

    fn case_to_input(self, case: &FuzzCase) -> Vec<u8> {
        match (self, case) {
            (Self::TinyAlu, FuzzCase::TinyAlu(case)) => case.to_input(),
            (Self::Aes, FuzzCase::Aes(case)) => case.to_input(),
            (Self::Sha256, FuzzCase::Sha256(case)) => case.to_input(),
            _ => unreachable!("target and case kind must match"),
        }
    }

    fn observe_input(self, bytes: &[u8]) -> ExitKind {
        signals_clear();
        signals_set(0);
        match self.decode_case(bytes) {
            Some(FuzzCase::TinyAlu(case)) => observe_tinyalu(case),
            Some(FuzzCase::Aes(case)) => observe_aes(&case),
            Some(FuzzCase::Sha256(case)) => observe_sha256(&case),
            _ => signals_set(SIGNALS_LEN - 1),
        }
        ExitKind::Ok
    }

    fn mandatory_cases(self) -> Vec<ExportCase> {
        match self {
            Self::TinyAlu => tinyalu_mandatory_cases(),
            Self::Aes => aes_mandatory_cases(),
            Self::Sha256 => sha256_mandatory_cases(),
        }
    }

    fn pseudo_random_seed_cases(self, seed: u64, count: usize) -> Vec<ExportCase> {
        let mut rng = Lcg::new(seed);
        match self {
            Self::TinyAlu => (0..count)
                .map(|idx| {
                    export_case(
                        FuzzCase::TinyAlu(TinyAluCase {
                            a: biased_byte(&mut rng, idx),
                            b: biased_byte(&mut rng, idx + 3),
                            op: TINYALU_OPS[(rng.next_u64() as usize) % TINYALU_OPS.len()],
                        }),
                        "libafl_seed",
                    )
                })
                .collect(),
            Self::Aes => (0..count)
                .map(|idx| {
                    let key_len = if rng.next_u64() & 1 == 0 { 128 } else { 256 };
                    let key_size = if key_len == 128 { 16 } else { 32 };
                    let mut key = vec![0; key_size];
                    for (byte_idx, byte) in key.iter_mut().enumerate() {
                        *byte = biased_byte(&mut rng, idx + byte_idx);
                    }
                    let mut block = [0u8; 16];
                    for (byte_idx, byte) in block.iter_mut().enumerate() {
                        *byte = biased_byte(&mut rng, idx + byte_idx + 11);
                    }
                    export_case(
                        FuzzCase::Aes(AesCase {
                            key_len,
                            encdec: if rng.next_u64() & 1 == 0 {
                                AesDirection::Encipher
                            } else {
                                AesDirection::Decipher
                            },
                            key,
                            block,
                        }),
                        "libafl_seed",
                    )
                })
                .collect(),
            Self::Sha256 => (0..count)
                .map(|idx| {
                    let len = (rng.next_u64() as usize) % 128;
                    let mut message = Vec::with_capacity(len);
                    for byte_idx in 0..len {
                        message.push(biased_byte(&mut rng, idx + byte_idx));
                    }
                    export_case(
                        FuzzCase::Sha256(Sha256Case {
                            mode: if rng.next_u64() & 1 == 0 {
                                ShaMode::Sha256
                            } else {
                                ShaMode::Sha224
                            },
                            message,
                        }),
                        "libafl_seed",
                    )
                })
                .collect(),
        }
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
            op: TINYALU_OPS[usize::from(bytes[2] % TINYALU_OPS.len() as u8)],
        })
    }

    fn to_input(self) -> Vec<u8> {
        let op_selector = TINYALU_OPS
            .iter()
            .position(|op| *op == self.op)
            .unwrap_or(0) as u8;
        vec![self.a, self.b, op_selector]
    }
}

impl AesCase {
    fn decode(bytes: &[u8]) -> Option<Self> {
        if bytes.len() < 50 {
            return None;
        }
        let key_len = if bytes[0] & 1 == 0 { 128 } else { 256 };
        let key_size = if key_len == 128 { 16 } else { 32 };
        let key = bytes[2..2 + key_size].to_vec();
        let mut block = [0u8; 16];
        block.copy_from_slice(&bytes[34..50]);
        Some(Self {
            key_len,
            encdec: if bytes[1] & 1 == 0 {
                AesDirection::Encipher
            } else {
                AesDirection::Decipher
            },
            key,
            block,
        })
    }

    fn to_input(&self) -> Vec<u8> {
        let mut input = vec![if self.key_len == 128 { 0 } else { 1 }, self.encdec.bit()];
        let mut key = [0u8; 32];
        key[..self.key.len()].copy_from_slice(&self.key);
        input.extend_from_slice(&key);
        input.extend_from_slice(&self.block);
        input
    }
}

impl AesDirection {
    fn bit(self) -> u8 {
        match self {
            Self::Encipher => 0,
            Self::Decipher => 1,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Encipher => "encipher",
            Self::Decipher => "decipher",
        }
    }
}

impl Sha256Case {
    fn decode(bytes: &[u8]) -> Option<Self> {
        if bytes.len() < 2 {
            return None;
        }
        let max_len = bytes.len().saturating_sub(2).min(127);
        let len = usize::from(bytes[1]).min(max_len);
        Some(Self {
            mode: if bytes[0] & 1 == 0 {
                ShaMode::Sha256
            } else {
                ShaMode::Sha224
            },
            message: bytes[2..2 + len].to_vec(),
        })
    }

    fn to_input(&self) -> Vec<u8> {
        let mut input = vec![self.mode.bit(), self.message.len().min(127) as u8];
        input.extend_from_slice(&self.message[..self.message.len().min(127)]);
        input
    }
}

impl ShaMode {
    fn bit(self) -> u8 {
        match self {
            Self::Sha256 => 0,
            Self::Sha224 => 1,
        }
    }

    fn name(self) -> &'static str {
        match self {
            Self::Sha256 => "sha256",
            Self::Sha224 => "sha224",
        }
    }
}

fn observe_tinyalu(case: TinyAluCase) {
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
    if matches!(
        (case.a, case.b),
        (0x55, 0xaa) | (0xaa, 0x55) | (0x0f, 0xf0) | (0xf0, 0x0f)
    ) {
        signals_set(13);
    }
    if case.a == case.b {
        signals_set(14);
    }
    if case.a ^ case.b == 0xff {
        signals_set(15);
    }
}

fn observe_aes(case: &AesCase) {
    signals_set(if case.key_len == 128 { 20 } else { 21 });
    signals_set(match case.encdec {
        AesDirection::Encipher => 22,
        AesDirection::Decipher => 23,
    });
    if case.key.iter().all(|byte| *byte == 0) {
        signals_set(24);
    }
    if case.block.iter().all(|byte| *byte == 0) {
        signals_set(25);
    }
    if case.key.iter().all(|byte| *byte == 0xff) || case.block.iter().all(|byte| *byte == 0xff) {
        signals_set(26);
    }
    if case.block.windows(2).any(|pair| pair[0] == pair[1]) {
        signals_set(27);
    }
}

fn observe_sha256(case: &Sha256Case) {
    signals_set(match case.mode {
        ShaMode::Sha256 => 32,
        ShaMode::Sha224 => 33,
    });
    match case.message.len() {
        0 => signals_set(34),
        1..=55 => signals_set(35),
        56..=64 => signals_set(36),
        _ => signals_set(37),
    }
    if case.message.iter().all(|byte| *byte == 0) {
        signals_set(38);
    }
    if case.message.iter().any(|byte| *byte >= 0x80) {
        signals_set(39);
    }
}

fn tinyalu_mandatory_cases() -> Vec<ExportCase> {
    let mut cases = Vec::new();
    for op in TINYALU_OPS {
        cases.push(export_case(
            FuzzCase::TinyAlu(TinyAluCase { a: 0, b: 0, op }),
            "mandatory_op",
        ));
        cases.push(export_case(
            FuzzCase::TinyAlu(TinyAluCase {
                a: 0xff,
                b: 0xff,
                op,
            }),
            "mandatory_max",
        ));
    }
    for op in TINYALU_OPS {
        for (a, b) in [
            (0x00, 0xff),
            (0xff, 0x00),
            (0x7f, 0x80),
            (0x55, 0xaa),
            (0xaa, 0x55),
        ] {
            cases.push(export_case(
                FuzzCase::TinyAlu(TinyAluCase { a, b, op }),
                "directed_pair",
            ));
        }
    }
    cases
}

fn aes_mandatory_cases() -> Vec<ExportCase> {
    let vectors = [
        (
            128,
            AesDirection::Encipher,
            hex_bytes("2b7e151628aed2a6abf7158809cf4f3c"),
            hex_block("6bc1bee22e409f96e93d7e117393172a"),
            "nist_aes128_enc",
        ),
        (
            128,
            AesDirection::Decipher,
            hex_bytes("2b7e151628aed2a6abf7158809cf4f3c"),
            hex_block("3ad77bb40d7a3660a89ecaf32466ef97"),
            "nist_aes128_dec",
        ),
        (
            256,
            AesDirection::Encipher,
            hex_bytes("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"),
            hex_block("6bc1bee22e409f96e93d7e117393172a"),
            "nist_aes256_enc",
        ),
        (
            256,
            AesDirection::Decipher,
            hex_bytes("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"),
            hex_block("f3eed1bdb5d2a03c064b5a7e3db181f8"),
            "nist_aes256_dec",
        ),
    ];

    vectors
        .into_iter()
        .map(|(key_len, encdec, key, block, origin)| {
            export_case(
                FuzzCase::Aes(AesCase {
                    key_len,
                    encdec,
                    key,
                    block,
                }),
                origin,
            )
        })
        .collect()
}

fn sha256_mandatory_cases() -> Vec<ExportCase> {
    [
        (ShaMode::Sha256, b"".as_slice(), "sha256_empty"),
        (ShaMode::Sha256, b"abc".as_slice(), "sha256_abc"),
        (ShaMode::Sha224, b"".as_slice(), "sha224_empty"),
        (ShaMode::Sha224, b"abc".as_slice(), "sha224_abc"),
        (
            ShaMode::Sha256,
            b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq".as_slice(),
            "sha256_two_blocks",
        ),
    ]
    .into_iter()
    .map(|(mode, message, origin)| {
        export_case(
            FuzzCase::Sha256(Sha256Case {
                mode,
                message: message.to_vec(),
            }),
            origin,
        )
    })
    .collect()
}

fn export_case(case: FuzzCase, origin: &str) -> ExportCase {
    ExportCase {
        case,
        origin: origin.to_string(),
    }
}

fn write_jsonl(path: &Path, cases: &[ExportCase]) -> Result<usize, Box<dyn Error>> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = File::create(path)?;
    for case in cases {
        serde_json::to_writer(&mut file, &case.to_json())?;
        writeln!(file)?;
    }
    Ok(cases.len())
}

impl ExportCase {
    fn to_json(&self) -> Value {
        match &self.case {
            FuzzCase::TinyAlu(case) => json!({
                "target": "tinyalu",
                "a": case.a,
                "b": case.b,
                "op": case.op,
                "origin": self.origin,
            }),
            FuzzCase::Aes(case) => json!({
                "target": "aes",
                "key_len": case.key_len,
                "encdec": case.encdec.name(),
                "key": bytes_to_hex(&case.key),
                "block": bytes_to_hex(&case.block),
                "origin": self.origin,
            }),
            FuzzCase::Sha256(case) => json!({
                "target": "sha256",
                "mode": case.mode.name(),
                "message": bytes_to_hex(&case.message),
                "origin": self.origin,
            }),
        }
    }
}

fn dedupe_cases(cases: Vec<ExportCase>) -> Vec<ExportCase> {
    let mut seen = BTreeSet::new();
    let mut deduped = Vec::new();
    for case in cases {
        if seen.insert(case.case.clone()) {
            deduped.push(case);
        }
    }
    deduped
}

fn load_tinyalu_directive_cases(path: &Path) -> Result<Vec<ExportCase>, Box<dyn Error>> {
    let text = fs::read_to_string(path)?;
    let value: Value = serde_json::from_str(&text)?;
    Ok(tinyalu_directive_cases_from_value(&value))
}

fn tinyalu_directive_cases_from_value(value: &Value) -> Vec<ExportCase> {
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
        if pairs.is_empty() {
            pairs.extend([(0x00, 0x00), (0xff, 0xff), (0x55, 0xaa), (0x7f, 0x80)]);
        }

        for op in ops {
            for (a, b) in &pairs {
                cases.push(export_case(
                    FuzzCase::TinyAlu(TinyAluCase { a: *a, b: *b, op }),
                    &origin,
                ));
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
        return TINYALU_OPS.to_vec();
    };

    let mut resolved = Vec::new();
    match raw_ops {
        Value::Array(items) => {
            for item in items {
                if let Some(op) = parse_op_value(item) {
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
        TINYALU_OPS.to_vec()
    } else {
        resolved.sort_unstable();
        resolved.dedup();
        resolved
    }
}

fn parse_op_value(value: &Value) -> Option<u8> {
    match value {
        Value::String(text) => match text.to_ascii_uppercase().as_str() {
            "ADD" => Some(1),
            "AND" => Some(2),
            "XOR" => Some(3),
            "MUL" => Some(4),
            _ => parse_u8_text(text).filter(|op| TINYALU_OPS.contains(op)),
        },
        _ => parse_u8_value(value).filter(|op| TINYALU_OPS.contains(op)),
    }
}

fn parse_u8_value(value: &Value) -> Option<u8> {
    match value {
        Value::Number(number) => number.as_u64().and_then(|raw| u8::try_from(raw).ok()),
        Value::String(text) => parse_u8_text(text),
        _ => None,
    }
}

fn parse_u8_text(text: &str) -> Option<u8> {
    let text = text.trim();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        u8::from_str_radix(hex, 16).ok()
    } else {
        text.parse().ok()
    }
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

fn biased_byte(rng: &mut Lcg, idx: usize) -> u8 {
    if idx % 3 == 0 {
        BYTE_EDGES[(rng.next_u64() as usize) % BYTE_EDGES.len()]
    } else {
        rng.next_u8()
    }
}

fn hex_bytes(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|idx| u8::from_str_radix(&text[idx..idx + 2], 16).expect("valid hex fixture"))
        .collect()
}

fn hex_block(text: &str) -> [u8; 16] {
    let bytes = hex_bytes(text);
    let mut block = [0u8; 16];
    block.copy_from_slice(&bytes);
    block
}

fn bytes_to_hex<T: AsRef<[u8]>>(bytes: T) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn print_help() {
    println!(
        "\
LibAFL + BFM corpus generator

Options:
  --target NAME       Target: tinyalu, aes, sha256 [default: tinyalu]
  --corpus-out PATH   JSONL corpus to write [default: coverage/<target>_corpus.jsonl]
  --crashes-dir PATH  LibAFL objective corpus directory [default: crashes]
  --directives PATH   TinyALU coverage_feedback.py mutation directives JSON
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_decode_keeps_tinyalu_legal() {
        let Some(FuzzCase::TinyAlu(case)) = Target::TinyAlu.decode_case(&[1, 2, 99]) else {
            panic!("decode failed");
        };
        assert_eq!(case.a, 1);
        assert_eq!(case.b, 2);
        assert!(TINYALU_OPS.contains(&case.op));
    }

    #[test]
    fn aes_decode_selects_key_size() {
        let mut input = vec![1, 0];
        input.extend(0u8..32);
        input.extend(32u8..48);
        let Some(FuzzCase::Aes(case)) = Target::Aes.decode_case(&input) else {
            panic!("decode failed");
        };
        assert_eq!(case.key_len, 256);
        assert_eq!(case.key.len(), 32);
        assert_eq!(case.block[0], 32);
    }

    #[test]
    fn sha_decode_bounds_message_length() {
        let Some(FuzzCase::Sha256(case)) = Target::Sha256.decode_case(&[0, 200, b'a', b'b']) else {
            panic!("decode failed");
        };
        assert_eq!(case.mode, ShaMode::Sha256);
        assert_eq!(case.message, b"ab");
    }

    #[test]
    fn mandatory_cases_cover_all_targets() {
        assert!(!Target::TinyAlu.mandatory_cases().is_empty());
        assert!(!Target::Aes.mandatory_cases().is_empty());
        assert!(!Target::Sha256.mandatory_cases().is_empty());
    }
}
