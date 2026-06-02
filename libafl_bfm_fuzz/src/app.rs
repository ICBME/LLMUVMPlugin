use std::{
    collections::{BTreeMap, BTreeSet, hash_map::DefaultHasher},
    env,
    error::Error,
    fs::{self, File},
    hash::{Hash, Hasher},
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
use serde::Deserialize;
use serde_json::{Map, Value};

const BYTE_EDGES: [u8; 8] = [0x00, 0x01, 0x02, 0x03, 0x7f, 0x80, 0xfe, 0xff];
const DEFAULT_INPUT_LEN: usize = 256;
const MAX_DIRECTIVE_CASES: usize = 256;
const MAX_HEX_LEN: usize = 4096;
const SIGNALS_LEN: usize = 64;

static mut SIGNALS: [u8; SIGNALS_LEN] = [0; SIGNALS_LEN];
static mut SIGNALS_PTR: *mut u8 = &raw mut SIGNALS as _;

#[derive(Clone, Debug)]
struct TargetSchema {
    name: String,
    fields: Vec<FieldSchema>,
}

#[derive(Clone, Debug)]
struct FieldSchema {
    name: String,
    kind: FieldKind,
    minimum: Option<i64>,
    maximum: Option<i64>,
    choices: Vec<CaseValue>,
    hex_len: Option<usize>,
    hex_len_by: BTreeMap<String, BTreeMap<String, usize>>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum FieldKind {
    Int,
    Enum,
    Hex,
    Any,
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
enum CaseValue {
    Int(i64),
    Text(String),
    Hex(Vec<u8>),
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct GenericCase {
    values: BTreeMap<String, CaseValue>,
}

#[derive(Clone, Debug)]
struct ExportCase {
    case: GenericCase,
    origin: String,
}

#[derive(Debug)]
struct Config {
    target: String,
    target_config: Option<PathBuf>,
    corpus_out: PathBuf,
    corpus_overridden: bool,
    crashes_dir: PathBuf,
    directives_path: Option<PathBuf>,
    iters: u64,
    max_seed_cases: usize,
    seed: u64,
}

#[derive(Debug, Deserialize)]
struct TargetConfigFile {
    name: Option<String>,
    #[serde(default)]
    field: Vec<FieldConfig>,
}

#[derive(Debug, Deserialize)]
struct FieldConfig {
    name: String,
    #[serde(default = "default_field_kind")]
    kind: String,
    #[serde(default, rename = "min")]
    minimum: Option<i64>,
    #[serde(default, rename = "max")]
    maximum: Option<i64>,
    #[serde(default)]
    choices: Vec<toml::Value>,
    #[serde(default)]
    hex_len: Option<usize>,
    #[serde(default)]
    hex_len_by: BTreeMap<String, BTreeMap<String, usize>>,
}

impl Default for Config {
    fn default() -> Self {
        let target = env::var("FUZZ_TARGET").unwrap_or_else(|_| "dut".to_string());
        Self {
            corpus_out: PathBuf::from(format!("coverage/{target}_corpus.jsonl")),
            target,
            target_config: env::var_os("FUZZ_TARGET_CONFIG").map(PathBuf::from),
            corpus_overridden: false,
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
                "--target" => {
                    config.target = require_value(&mut args, "--target")?;
                }
                "--target-config" => {
                    config.target_config =
                        Some(PathBuf::from(require_value(&mut args, "--target-config")?));
                }
                "--corpus-out" => {
                    config.corpus_out = PathBuf::from(require_value(&mut args, "--corpus-out")?);
                    config.corpus_overridden = true;
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

        if let Ok(path) = env::var("LIBAFL_DIRECTIVES")
            && config.directives_path.is_none()
        {
            config.directives_path = Some(PathBuf::from(path));
        }
        if let Ok(path) = env::var("LIBAFL_CORPUS_OUT") {
            config.corpus_out = PathBuf::from(path);
            config.corpus_overridden = true;
        }

        Ok(Some(config))
    }
}

pub fn main_entry() {
    if let Err(err) = run() {
        eprintln!("libafl_bfm_fuzz: {err}");
        std::process::exit(1);
    }
}

fn run() -> Result<(), Box<dyn Error>> {
    let Some(config) = Config::from_args()? else {
        return Ok(());
    };
    let schema = TargetSchema::load(&config.target, config.target_config.as_deref())?;
    let corpus_out = if config.corpus_overridden {
        config.corpus_out.clone()
    } else {
        PathBuf::from(format!("coverage/{}_corpus.jsonl", schema.name))
    };

    let mandatory = schema.mandatory_cases();
    let directed = match &config.directives_path {
        Some(path) => load_directive_cases(&schema, path)?,
        None => Vec::new(),
    };
    let random_seeds = schema.pseudo_random_seed_cases(config.seed, config.max_seed_cases);

    let seed_inputs: Vec<BytesInput> = mandatory
        .iter()
        .chain(directed.iter())
        .chain(random_seeds.iter())
        .map(|case| BytesInput::new(case.case.to_seed_input()))
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

    let observed_schema = schema.clone();
    let mut harness = |input: &BytesInput| {
        let bytes = input.target_bytes();
        observed_schema.observe_input(bytes.as_slice())
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
        export_cases.push(ExportCase {
            case: schema.decode_case(input.as_ref()),
            origin: "libafl_corpus".to_string(),
        });
    }

    let written = write_jsonl(&corpus_out, &schema, &dedupe_cases(export_cases))?;
    println!(
        "LibAFL {} corpus: wrote {written} cases to {}",
        schema.name,
        corpus_out.display()
    );
    Ok(())
}

impl TargetSchema {
    fn load(target: &str, explicit_path: Option<&Path>) -> Result<Self, Box<dyn Error>> {
        let path = resolve_target_config_path(target, explicit_path)?;
        let text = fs::read_to_string(&path)?;
        Self::from_toml(target, &text).map_err(|err| format!("{}: {err}", path.display()).into())
    }

    fn from_toml(target: &str, text: &str) -> Result<Self, Box<dyn Error>> {
        let raw: TargetConfigFile = toml::from_str(text)?;
        if raw.field.is_empty() {
            return Err("target config must define at least one [[field]]".into());
        }
        let fields = raw
            .field
            .into_iter()
            .map(FieldSchema::from_config)
            .collect::<Result<Vec<_>, _>>()?;
        Ok(Self {
            name: raw.name.unwrap_or_else(|| target.to_string()),
            fields,
        })
    }

    fn decode_case(&self, bytes: &[u8]) -> GenericCase {
        let mut cursor = ByteCursor::new(bytes);
        let mut case = GenericCase {
            values: BTreeMap::new(),
        };
        for field in &self.fields {
            let value = field.decode(&mut cursor, &case.values);
            case.values.insert(field.name.clone(), value);
        }
        self.normalize_case(&mut case);
        case
    }

    fn default_case(&self) -> GenericCase {
        let mut case = GenericCase {
            values: BTreeMap::new(),
        };
        for field in &self.fields {
            let value = field.default_value(&case.values);
            case.values.insert(field.name.clone(), value);
        }
        self.normalize_case(&mut case);
        case
    }

    fn mandatory_cases(&self) -> Vec<ExportCase> {
        let base = self.default_case();
        let mut cases = vec![export_case(base.clone(), "schema_default")];
        for field in &self.fields {
            for value in field.interesting_values(&base.values) {
                let mut case = base.clone();
                case.values.insert(field.name.clone(), value);
                self.normalize_case(&mut case);
                cases.push(export_case(case, "schema_edge"));
            }
        }
        dedupe_cases(cases)
    }

    fn pseudo_random_seed_cases(&self, seed: u64, count: usize) -> Vec<ExportCase> {
        let mut rng = Lcg::new(seed);
        (0..count)
            .map(|idx| {
                let mut input = vec![0; DEFAULT_INPUT_LEN];
                for (byte_idx, byte) in input.iter_mut().enumerate() {
                    *byte = biased_byte(&mut rng, idx + byte_idx);
                }
                export_case(self.decode_case(&input), "libafl_seed")
            })
            .collect()
    }

    fn observe_input(&self, bytes: &[u8]) -> ExitKind {
        signals_clear();
        signals_set(0);
        let case = self.decode_case(bytes);
        for (name, value) in &case.values {
            signals_set(1 + stable_slot(name, value) % (SIGNALS_LEN - 1));
        }
        ExitKind::Ok
    }

    fn normalize_case(&self, case: &mut GenericCase) {
        let mut updates = Vec::new();
        for field in &self.fields {
            let value = match case.values.get(&field.name).cloned() {
                Some(value) if field.accepts(&value) => field.normalize_value(value, &case.values),
                _ => field.default_value(&case.values),
            };
            updates.push((field.name.clone(), value));
        }
        for (name, value) in updates {
            case.values.insert(name, value);
        }
    }

    fn directive_cases_from_value(&self, value: &Value) -> Vec<ExportCase> {
        let mut cases = Vec::new();
        for (idx, directive) in directive_items(value).iter().enumerate() {
            if directive.get("enabled").and_then(Value::as_bool) == Some(false) {
                continue;
            }
            let origin = directive_origin(directive, idx);
            for case in self.explicit_cases(directive) {
                cases.push(export_case(case, &origin));
                if cases.len() >= MAX_DIRECTIVE_CASES {
                    return cases;
                }
            }

            let overlays = self.directive_overlays(directive);
            if overlays.is_empty() {
                continue;
            }
            let mut expanded = vec![self.default_case()];
            for field in &self.fields {
                let Some(values) = overlays.get(&field.name) else {
                    continue;
                };
                let mut next = Vec::new();
                for case in &expanded {
                    for value in values {
                        let mut case = case.clone();
                        case.values.insert(field.name.clone(), value.clone());
                        self.normalize_case(&mut case);
                        next.push(case);
                        if next.len() >= MAX_DIRECTIVE_CASES {
                            break;
                        }
                    }
                    if next.len() >= MAX_DIRECTIVE_CASES {
                        break;
                    }
                }
                expanded = next;
            }
            for case in expanded {
                cases.push(export_case(case, &origin));
                if cases.len() >= MAX_DIRECTIVE_CASES {
                    return cases;
                }
            }
        }
        cases
    }

    fn explicit_cases(&self, directive: &Value) -> Vec<GenericCase> {
        let items = directive
            .get("cases")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_else(|| {
                if self
                    .fields
                    .iter()
                    .any(|field| directive.get(&field.name).is_some())
                {
                    std::slice::from_ref(directive)
                } else {
                    &[]
                }
            });
        let mut cases = Vec::new();
        for item in items {
            let mut case = self.default_case();
            let mut seen_field = false;
            for field in &self.fields {
                if let Some(value) = item
                    .get(&field.name)
                    .and_then(|value| field.value_from_json(value, &case.values))
                {
                    case.values.insert(field.name.clone(), value);
                    seen_field = true;
                }
            }
            if seen_field {
                self.normalize_case(&mut case);
                cases.push(case);
            }
        }
        cases
    }

    fn directive_overlays(&self, directive: &Value) -> BTreeMap<String, Vec<CaseValue>> {
        let mut overlays = BTreeMap::new();
        let base = self.default_case();
        for field in &self.fields {
            let mut values = Vec::new();
            for key in [&field.name, &format!("{}_values", field.name)] {
                for item in parse_array_or_one(directive.get(key).unwrap_or(&Value::Null)) {
                    if let Some(value) = field.value_from_json(item, &base.values) {
                        values.push(value);
                    }
                }
            }
            if field.kind == FieldKind::Hex {
                let pattern_key = format!("{}_patterns", field.name);
                for item in parse_array_or_one(directive.get(&pattern_key).unwrap_or(&Value::Null))
                {
                    if let Some(pattern) = item.as_str() {
                        let len = field.hex_len(&base.values).unwrap_or(16);
                        values.push(CaseValue::Hex(pattern_bytes(pattern, len)));
                    }
                }
            }
            dedupe_values(&mut values);
            if !values.is_empty() {
                overlays.insert(field.name.clone(), values);
            }
        }
        overlays
    }
}

impl FieldSchema {
    fn from_config(config: FieldConfig) -> Result<Self, Box<dyn Error>> {
        let kind = FieldKind::parse(&config.kind);
        let choices = config
            .choices
            .iter()
            .filter_map(|value| kind.choice_from_toml(value))
            .collect();
        Ok(Self {
            name: config.name,
            kind,
            minimum: config.minimum,
            maximum: config.maximum,
            choices,
            hex_len: config.hex_len,
            hex_len_by: config.hex_len_by,
        })
    }

    fn decode(
        &self,
        cursor: &mut ByteCursor<'_>,
        values: &BTreeMap<String, CaseValue>,
    ) -> CaseValue {
        match self.kind {
            FieldKind::Int => {
                if !self.choices.is_empty() {
                    return self.choices[(cursor.next_u64() as usize) % self.choices.len()].clone();
                }
                let min = self.minimum.unwrap_or(0);
                let max = self.maximum.unwrap_or(min.saturating_add(255));
                let (lo, hi) = if min <= max { (min, max) } else { (max, min) };
                let span = (i128::from(hi) - i128::from(lo) + 1).min(i128::from(u64::MAX)) as u64;
                let offset = if span == 0 {
                    0
                } else {
                    cursor.next_u64() % span
                };
                CaseValue::Int(lo.saturating_add(offset as i64))
            }
            FieldKind::Enum => {
                if self.choices.is_empty() {
                    CaseValue::Text("default".to_string())
                } else {
                    self.choices[(cursor.next_u64() as usize) % self.choices.len()].clone()
                }
            }
            FieldKind::Hex => {
                let len = self
                    .hex_len(values)
                    .unwrap_or_else(|| usize::from(cursor.next_u8() % 32));
                CaseValue::Hex(cursor.take(len.min(MAX_HEX_LEN)))
            }
            FieldKind::Any => CaseValue::Text(cursor.next_u64().to_string()),
        }
    }

    fn default_value(&self, values: &BTreeMap<String, CaseValue>) -> CaseValue {
        match self.kind {
            FieldKind::Int => self
                .choices
                .first()
                .cloned()
                .unwrap_or_else(|| CaseValue::Int(self.minimum.unwrap_or(0))),
            FieldKind::Enum => self
                .choices
                .first()
                .cloned()
                .unwrap_or_else(|| CaseValue::Text("default".to_string())),
            FieldKind::Hex => CaseValue::Hex(vec![0; self.hex_len(values).unwrap_or(16)]),
            FieldKind::Any => CaseValue::Text("default".to_string()),
        }
    }

    fn interesting_values(&self, values: &BTreeMap<String, CaseValue>) -> Vec<CaseValue> {
        let mut result = match self.kind {
            FieldKind::Int | FieldKind::Enum if !self.choices.is_empty() => self.choices.clone(),
            FieldKind::Int => {
                let min = self.minimum.unwrap_or(0);
                let max = self.maximum.unwrap_or(min.saturating_add(255));
                let mut values = vec![CaseValue::Int(min), CaseValue::Int(max)];
                if min <= 0 && 0 <= max {
                    values.push(CaseValue::Int(0));
                }
                values
            }
            FieldKind::Enum => vec![CaseValue::Text("default".to_string())],
            FieldKind::Hex => {
                let len = self.hex_len(values).unwrap_or(16).min(MAX_HEX_LEN);
                vec![
                    CaseValue::Hex(vec![0; len]),
                    CaseValue::Hex(vec![0xff; len]),
                    CaseValue::Hex((0..len).map(|idx| idx as u8).collect()),
                ]
            }
            FieldKind::Any => vec![
                CaseValue::Text("default".to_string()),
                CaseValue::Text("alt".to_string()),
            ],
        };
        dedupe_values(&mut result);
        result
    }

    fn value_from_json(
        &self,
        value: &Value,
        current_values: &BTreeMap<String, CaseValue>,
    ) -> Option<CaseValue> {
        let parsed = match self.kind {
            FieldKind::Int => match value {
                Value::Number(number) => number.as_i64().map(CaseValue::Int),
                Value::String(text) => parse_i64_text(text).map(CaseValue::Int),
                _ => None,
            },
            FieldKind::Enum => scalar_to_string(value).map(CaseValue::Text),
            FieldKind::Hex => match value {
                Value::String(text) => {
                    if let Some(pattern) = text.strip_prefix("pattern:") {
                        let len = self.hex_len(current_values).unwrap_or(16);
                        Some(CaseValue::Hex(pattern_bytes(pattern, len)))
                    } else {
                        parse_hex_bytes(text).map(CaseValue::Hex)
                    }
                }
                Value::Object(map) => map.get("pattern").and_then(Value::as_str).map(|pattern| {
                    CaseValue::Hex(pattern_bytes(
                        pattern,
                        self.hex_len(current_values).unwrap_or(16),
                    ))
                }),
                _ => None,
            },
            FieldKind::Any => scalar_to_string(value).map(CaseValue::Text),
        }?;
        if self.choices.is_empty() || self.choices.contains(&parsed) {
            Some(self.normalize_value(parsed, current_values))
        } else {
            None
        }
    }

    fn accepts(&self, value: &CaseValue) -> bool {
        matches!(
            (self.kind, value),
            (FieldKind::Int, CaseValue::Int(_))
                | (FieldKind::Enum, CaseValue::Text(_))
                | (FieldKind::Hex, CaseValue::Hex(_))
                | (FieldKind::Any, _)
        )
    }

    fn normalize_value(
        &self,
        value: CaseValue,
        current_values: &BTreeMap<String, CaseValue>,
    ) -> CaseValue {
        match (self.kind, value) {
            (FieldKind::Int, CaseValue::Int(raw)) => {
                if !self.choices.is_empty() {
                    return CaseValue::Int(raw);
                }
                let min = self.minimum.unwrap_or(i64::MIN);
                let max = self.maximum.unwrap_or(i64::MAX);
                CaseValue::Int(raw.clamp(min, max))
            }
            (FieldKind::Hex, CaseValue::Hex(mut bytes)) => {
                if let Some(len) = self.hex_len(current_values) {
                    bytes.resize(len.min(MAX_HEX_LEN), 0);
                }
                CaseValue::Hex(bytes)
            }
            (_, value) => value,
        }
    }

    fn hex_len(&self, values: &BTreeMap<String, CaseValue>) -> Option<usize> {
        if let Some(len) = self.hex_len {
            return Some(len);
        }
        for (selector, choices) in &self.hex_len_by {
            let Some(value) = values.get(selector) else {
                return choices.values().copied().max();
            };
            if let Some(len) = choices.get(&value.selector_key()) {
                return Some(*len);
            }
            return choices.values().copied().max();
        }
        None
    }
}

impl FieldKind {
    fn parse(text: &str) -> Self {
        match text.trim().to_ascii_lowercase().as_str() {
            "int" | "integer" => Self::Int,
            "enum" | "choice" | "choices" => Self::Enum,
            "hex" | "bytes" | "byte_string" => Self::Hex,
            _ => Self::Any,
        }
    }

    fn choice_from_toml(self, value: &toml::Value) -> Option<CaseValue> {
        match self {
            Self::Int => value
                .as_integer()
                .map(CaseValue::Int)
                .or_else(|| value.as_str().and_then(parse_i64_text).map(CaseValue::Int)),
            Self::Enum => toml_scalar_to_string(value).map(CaseValue::Text),
            Self::Hex => value.as_str().and_then(parse_hex_bytes).map(CaseValue::Hex),
            Self::Any => toml_scalar_to_string(value).map(CaseValue::Text),
        }
    }
}

impl CaseValue {
    fn to_json(&self) -> Value {
        match self {
            Self::Int(value) => Value::from(*value),
            Self::Text(value) => Value::String(value.clone()),
            Self::Hex(value) => Value::String(bytes_to_hex(value)),
        }
    }

    fn seed_bytes(&self) -> Vec<u8> {
        match self {
            Self::Int(value) => value.to_le_bytes().to_vec(),
            Self::Text(value) => value.as_bytes().to_vec(),
            Self::Hex(value) => value.clone(),
        }
    }

    fn selector_key(&self) -> String {
        match self {
            Self::Int(value) => value.to_string(),
            Self::Text(value) => value.clone(),
            Self::Hex(value) => bytes_to_hex(value),
        }
    }
}

impl GenericCase {
    fn to_json(&self, target: &str, origin: &str) -> Value {
        let mut map = Map::new();
        map.insert("target".to_string(), Value::String(target.to_string()));
        for (name, value) in &self.values {
            map.insert(name.clone(), value.to_json());
        }
        map.insert("origin".to_string(), Value::String(origin.to_string()));
        Value::Object(map)
    }

    fn to_seed_input(&self) -> Vec<u8> {
        let mut bytes = Vec::new();
        for (name, value) in &self.values {
            bytes.extend_from_slice(name.as_bytes());
            bytes.push(0);
            bytes.extend(value.seed_bytes());
            bytes.push(0xff);
        }
        if bytes.is_empty() { vec![0] } else { bytes }
    }
}

struct ByteCursor<'a> {
    bytes: &'a [u8],
    offset: usize,
}

impl<'a> ByteCursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, offset: 0 }
    }

    fn next_u8(&mut self) -> u8 {
        if self.bytes.is_empty() {
            self.offset = self.offset.saturating_add(1);
            return 0;
        }
        let value = self.bytes[self.offset % self.bytes.len()];
        self.offset = self.offset.saturating_add(1);
        value
    }

    fn next_u64(&mut self) -> u64 {
        let mut raw = [0u8; 8];
        for byte in &mut raw {
            *byte = self.next_u8();
        }
        u64::from_le_bytes(raw)
    }

    fn take(&mut self, len: usize) -> Vec<u8> {
        (0..len).map(|_| self.next_u8()).collect()
    }
}

fn resolve_target_config_path(
    target: &str,
    explicit_path: Option<&Path>,
) -> Result<PathBuf, Box<dyn Error>> {
    if let Some(path) = explicit_path {
        if !path.exists() {
            return Err(format!("target config does not exist: {}", path.display()).into());
        }
        return Ok(path.to_path_buf());
    }

    let root = env::var_os("FUZZ_TARGETS_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("targets"));
    let path = root.join(format!("{target}.toml"));
    if !path.exists() {
        return Err(format!(
            "target config not found for {target:?}: {}; set --target-config or FUZZ_TARGET_CONFIG",
            path.display()
        )
        .into());
    }
    Ok(path)
}

fn load_directive_cases(
    schema: &TargetSchema,
    path: &Path,
) -> Result<Vec<ExportCase>, Box<dyn Error>> {
    let text = fs::read_to_string(path)?;
    let value: Value = serde_json::from_str(&text)?;
    Ok(schema.directive_cases_from_value(&value))
}

fn export_case(case: GenericCase, origin: &str) -> ExportCase {
    ExportCase {
        case,
        origin: origin.to_string(),
    }
}

fn write_jsonl(
    path: &Path,
    schema: &TargetSchema,
    cases: &[ExportCase],
) -> Result<usize, Box<dyn Error>> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let mut file = File::create(path)?;
    for case in cases {
        serde_json::to_writer(&mut file, &case.case.to_json(&schema.name, &case.origin))?;
        writeln!(file)?;
    }
    Ok(cases.len())
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

fn dedupe_values(values: &mut Vec<CaseValue>) {
    let mut seen = BTreeSet::new();
    values.retain(|value| seen.insert(value.clone()));
}

fn directive_items(value: &Value) -> &[Value] {
    if let Some(items) = value.as_array() {
        items.as_slice()
    } else if let Some(items) = value.get("directives").and_then(Value::as_array) {
        items.as_slice()
    } else {
        &[]
    }
}

fn directive_origin(directive: &Value, idx: usize) -> String {
    directive
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_string)
        .unwrap_or_else(|| format!("directive_{idx}"))
}

fn parse_array_or_one(value: &Value) -> Vec<&Value> {
    match value {
        Value::Array(items) => items.iter().collect(),
        Value::Null => Vec::new(),
        other => vec![other],
    }
}

fn scalar_to_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.clone()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn toml_scalar_to_string(value: &toml::Value) -> Option<String> {
    value
        .as_str()
        .map(str::to_string)
        .or_else(|| value.as_integer().map(|item| item.to_string()))
        .or_else(|| value.as_bool().map(|item| item.to_string()))
}

fn parse_i64_text(text: &str) -> Option<i64> {
    let text = text.trim();
    if let Some(hex) = text.strip_prefix("0x").or_else(|| text.strip_prefix("0X")) {
        i64::from_str_radix(hex, 16).ok()
    } else {
        text.parse().ok()
    }
}

fn parse_hex_bytes(text: &str) -> Option<Vec<u8>> {
    let text = text.trim();
    let text = text.strip_prefix("0x").unwrap_or(text);
    if text.len() % 2 != 0 {
        return None;
    }
    (0..text.len())
        .step_by(2)
        .map(|idx| u8::from_str_radix(&text[idx..idx + 2], 16).ok())
        .collect()
}

fn pattern_bytes(pattern: &str, len: usize) -> Vec<u8> {
    match pattern.trim().to_ascii_lowercase().as_str() {
        "zero" | "zeros" => vec![0x00; len],
        "ff" | "ones" | "max" => vec![0xff; len],
        "alternating" | "aa55" => (0..len)
            .map(|idx| if idx % 2 == 0 { 0xaa } else { 0x55 })
            .collect(),
        "55aa" => (0..len)
            .map(|idx| if idx % 2 == 0 { 0x55 } else { 0xaa })
            .collect(),
        "walking_one" => (0..len).map(|idx| 1u8 << (idx % 8)).collect(),
        "increment" | "counter" => (0..len).map(|idx| idx as u8).collect(),
        "decrement" => (0..len).map(|idx| 0xffu8.wrapping_sub(idx as u8)).collect(),
        _ => (0..len).map(|idx| (idx as u8).wrapping_mul(17)).collect(),
    }
}

fn stable_slot(name: &str, value: &CaseValue) -> usize {
    let mut hasher = DefaultHasher::new();
    name.hash(&mut hasher);
    value.hash(&mut hasher);
    hasher.finish() as usize
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

fn bytes_to_hex<T: AsRef<[u8]>>(bytes: T) -> String {
    bytes
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn default_field_kind() -> String {
    "any".to_string()
}

fn print_help() {
    println!(
        "\
LibAFL + BFM corpus generator

Options:
  --target NAME          Target name used to resolve targets/<name>.toml [default: FUZZ_TARGET or dut]
  --target-config PATH   Target manifest with generic [[field]] schema
  --corpus-out PATH      JSONL corpus to write [default: coverage/<manifest-name>_corpus.jsonl]
  --crashes-dir PATH     LibAFL objective corpus directory [default: crashes]
  --directives PATH      Generic mutation directives JSON
  --iters N              LibAFL fuzz iterations [default: LIBAFL_ITERS or 256]
  --max-seeds N          deterministic random seed cases [default: LIBAFL_MAX_SEEDS or 32]
  --seed N               deterministic seed [default: LIBAFL_SEED or 1]
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

    fn schema_fixture() -> TargetSchema {
        TargetSchema::from_toml(
            "demo",
            r#"
name = "demo"

[[field]]
name = "mode"
kind = "enum"
choices = ["read", "write"]

[[field]]
name = "size"
kind = "int"
min = 1
max = 4
choices = [1, 2, 4]

[[field]]
name = "payload"
kind = "hex"
hex_len_by = { size = { "1" = 1, "2" = 2, "4" = 4 } }
"#,
        )
        .expect("schema")
    }

    #[test]
    fn generic_decode_respects_schema_choices_and_hex_lengths() {
        let schema = schema_fixture();
        let case = schema.decode_case(&[0, 0, 0, 0, 0, 0, 0, 0, 2, 0xaa, 0xbb, 0xcc]);

        assert_eq!(
            case.values.get("mode"),
            Some(&CaseValue::Text("read".to_string()))
        );
        assert!(matches!(
            case.values.get("size"),
            Some(CaseValue::Int(1 | 2 | 4))
        ));
        let Some(CaseValue::Hex(payload)) = case.values.get("payload") else {
            panic!("missing payload");
        };
        let size: usize = case
            .values
            .get("size")
            .unwrap()
            .selector_key()
            .parse()
            .unwrap();
        assert_eq!(payload.len(), size);
    }

    #[test]
    fn mandatory_cases_are_schema_driven() {
        let schema = schema_fixture();
        let cases = schema.mandatory_cases();

        assert!(cases.iter().any(|case| {
            case.case.values.get("mode") == Some(&CaseValue::Text("write".to_string()))
        }));
        assert!(cases.iter().any(|case| {
            matches!(case.case.values.get("payload"), Some(CaseValue::Hex(bytes)) if bytes.iter().all(|byte| *byte == 0xff))
        }));
    }

    #[test]
    fn directives_accept_explicit_generic_fields() {
        let schema = schema_fixture();
        let directives = serde_json::json!({
            "directives": [{
                "name": "directed",
                "cases": [{"mode": "write", "size": 4, "payload": "deadbeef"}]
            }]
        });
        let cases = schema.directive_cases_from_value(&directives);

        assert_eq!(cases.len(), 1);
        assert_eq!(
            cases[0].case.values.get("payload"),
            Some(&CaseValue::Hex(vec![0xde, 0xad, 0xbe, 0xef]))
        );
    }

    #[test]
    fn directives_skip_disabled_items() {
        let schema = schema_fixture();
        let directives = serde_json::json!({
            "directives": [
                {
                    "name": "disabled",
                    "enabled": false,
                    "cases": [{"mode": "write", "size": 4, "payload": "deadbeef"}]
                },
                {
                    "name": "enabled",
                    "cases": [{"mode": "read", "size": 1, "payload": "00"}]
                }
            ]
        });
        let cases = schema.directive_cases_from_value(&directives);

        assert_eq!(cases.len(), 1);
        assert_eq!(cases[0].origin, "enabled");
        assert_eq!(
            cases[0].case.values.get("payload"),
            Some(&CaseValue::Hex(vec![0x00]))
        );
    }
}
