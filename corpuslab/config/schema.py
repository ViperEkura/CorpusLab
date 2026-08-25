"""Declarative config schema (pydantic v2): field types, defaults, required
constraints.

Unknown keys are rejected (extra='forbid') and legacy aliases get migration
hints — dead switches take no seat (P2), aliases never enter the schema.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Legacy alias → suggested spelling (loader/validate use this for hints)
ALIASES: Dict[str, str] = {
    "total_count": "count",
    "target_count": "count",
    "samples_per_topic": "topics[].weight (ratio) + plan.count (total)",
    "two_stage": "(deleted: two-stage is the only implementation path)",
    "dry_run": "run.preview",
    "random_seed": "run.seed",
    "checkpoint_interval": "(deleted: the DuckDB state store is the checkpoint)",
    "max_retries": "retry.attempts",
    "auto_stop": "llm.breaker",
    "max_score": "max",
    "min_total_score": "min_total",
    "api_key_env": "api_key (leave empty to read the env var)",
    "base_url_env": "base_url (leave empty to read the env var)",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetryCfg(Strict):
    attempts: int = 3
    backoff: float = 2.0
    max_delay: float = 30.0


class BreakerCfg(Strict):
    window: float = 50.0
    max_retry_ratio: float = 0.9


class LlmCfg(Strict):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    lang: str = "en"
    concurrency: int = 1
    params: Dict[str, Any] = Field(default_factory=dict)
    retry: RetryCfg = Field(default_factory=RetryCfg)
    breaker: BreakerCfg = Field(default_factory=BreakerCfg)


class EmbeddingCfg(Strict):
    model: str = "text-embedding-v3"
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    batch_size: int = 32


class RunCfg(Strict):
    seed: Optional[int] = None
    preview: bool = False
    preview_count: int = 8


class PlanCfg(Strict):
    count: Optional[int] = None


class TopicItem(Strict):
    topic: str
    weight: float = 1.0
    knowledge: Optional[str] = None


class Dimension(Strict):
    name: str
    vals: List[str] = Field(default_factory=list)


class TopicDrivenCfg(Strict):
    type: Literal["topic_driven"]
    weight: float = 1.0
    count: Optional[int] = None
    topics: List[TopicItem]
    dimensions: List[Dimension] = Field(default_factory=lambda: [
        Dimension(name="difficulty", vals=["easy", "medium", "hard"]),
    ])
    multi_turn: Optional[bool] = None
    require_reasoning: bool = False


class DeepThinkingCfg(Strict):
    type: Literal["deep_thinking"]
    weight: float = 1.0
    count: Optional[int] = None
    topics: List[TopicItem]
    dimensions: List[Dimension] = Field(default_factory=lambda: [
        Dimension(name="difficulty", vals=["easy", "medium", "hard"]),
    ])
    multi_turn: Optional[bool] = None


class EvolutionCfg(Strict):
    crossover: float = 0.0
    mutate: float = 0.0
    mode: Literal["instruction_output", "compose"] = "instruction_output"
    mutations: List[Dict[str, Any]] = Field(default_factory=list)


class SeedDrivenCfg(Strict):
    type: Literal["seed_driven"]
    weight: float = 1.0
    count: Optional[int] = None
    field_map: Dict[str, str] = Field(default_factory=dict)
    seed_file: str
    example_num: int = 3
    topic: Optional[str] = None
    evolution: EvolutionCfg = Field(default_factory=EvolutionCfg)


class DepthMutation(Strict):
    name: str
    prompt: str


class EvolInstructCfg(Strict):
    type: Literal["evol_instruct"]
    weight: float = 1.0
    count: Optional[int] = None
    field_map: Dict[str, str] = Field(default_factory=dict)
    seed_file: str
    max_rounds: int = 3
    depth_rate: float = 0.7
    branch_factor: int = 1
    depth_mutations: List[DepthMutation] = Field(default_factory=lambda: [
        DepthMutation(name="add_constraint",
                      prompt="Add one more constraint to the instruction."),
        DepthMutation(name="deepen",
                      prompt="Make the instruction require deeper reasoning."),
        DepthMutation(name="concretize",
                      prompt="Make the instruction more concrete and specific."),
        DepthMutation(name="increase_reasoning",
                      prompt="Increase the reasoning steps required."),
    ])
    ratio_bounds: List[float] = Field(default_factory=lambda: [0.5, 5.0])
    generate_output: bool = True
    require_reasoning: bool = False
    include_seeds: bool = False
    phases: Dict[str, Dict[str, Any]] = Field(default_factory=dict)

    @field_validator("ratio_bounds")
    @classmethod
    def _bounds(cls, v):
        if len(v) != 2 or v[0] >= v[1]:
            raise ValueError("ratio_bounds must be [min, max] with min < max")
        return v


class ChunkingCfg(Strict):
    enabled: bool = False
    mode: Literal["structure", "semantic"] = "structure"
    min_chunk_length: int = 200
    max_chunk_length: int = 1500
    similarity_threshold: float = 0.55


class DocumentQACfg(Strict):
    type: Literal["document_qa"]
    weight: float = 1.0
    count: Optional[int] = None
    field_map: Dict[str, str] = Field(default_factory=dict)
    document_file: str
    chunking: ChunkingCfg = Field(default_factory=ChunkingCfg)
    max_instruction_length: int = 500
    max_output_length: int = 4000
    reject_context_references: bool = True


class BacktranslationCfg(Strict):
    type: Literal["instruction_backtranslation"]
    weight: float = 1.0
    count: Optional[int] = None
    field_map: Dict[str, str] = Field(default_factory=dict)
    document_file: str
    min_document_length: int = 50
    max_document_length: int = 4000
    max_instruction_length: int = 500
    reject_context_references: bool = True
    shuffle: bool = False
    phases: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class ToolCallCfg(Strict):
    type: Literal["tool_call"]
    weight: float = 1.0
    count: Optional[int] = None
    tools: List[Dict[str, Any]]
    topics: List[str] = Field(default_factory=lambda: [
        "data analysis", "schedule management", "information retrieval",
        "travel planning", "finance and budgeting",
    ])
    system_prompt: Optional[str] = None
    max_tool_calls_per_sample: int = 2


StrategyCfg = Union[
    TopicDrivenCfg, DeepThinkingCfg, SeedDrivenCfg, EvolInstructCfg,
    DocumentQACfg, BacktranslationCfg, ToolCallCfg,
]


class LengthStageCfg(Strict):
    type: Literal["length"]
    instruction: List[int] = Field(default_factory=lambda: [5, 4000])
    output: List[int] = Field(default_factory=lambda: [10, 8000])


class ExactDedupStageCfg(Strict):
    type: Literal["exact_dedup"]


class StatsStageCfg(Strict):
    type: Literal["stats"]
    max_special_char_ratio: float = 0.3
    max_word_repetition: float = 0.5
    max_char_repetition: float = 0.5
    min_ngram_diversity: float = 0.2
    ngram_n: int = 3
    unit: Literal["char", "word"] = "char"


class MinHashStageCfg(Strict):
    type: Literal["minhash_dedup"]
    threshold: float = 0.7
    num_perm: int = 128
    ngram_n: int = 3


class SemanticDedupStageCfg(Strict):
    type: Literal["semantic_dedup"]
    threshold: float = 0.85


class ClusterDedupStageCfg(Strict):
    type: Literal["cluster_dedup"]


StageCfg = Union[
    LengthStageCfg, ExactDedupStageCfg, StatsStageCfg, MinHashStageCfg,
    SemanticDedupStageCfg, ClusterDedupStageCfg,
]

STREAMING_STAGES = {"length", "exact_dedup", "stats", "minhash_dedup"}
BATCH_STAGES = {"semantic_dedup", "cluster_dedup"}


class JudgeDimension(Strict):
    name: str
    label: Optional[str] = None
    max: float = 10.0


class JudgeRef(Strict):
    endpoint: str = "llm"


class ScorerCfg(Strict):
    type: str
    model_path: Optional[str] = None
    weight: float = 1.0
    dimensions: List[str] = Field(default_factory=list)


class JudgeCfg(Strict):
    endpoint: Optional[str] = None
    dimensions: List[JudgeDimension] = Field(default_factory=list)
    min_total: float = 0.0
    judges: List[JudgeRef] = Field(default_factory=list)
    aggregation: Literal["mean", "min", "max", "median"] = "mean"
    min_judges: int = 1
    max_disagreement: float = 0.0
    scorers: List[ScorerCfg] = Field(default_factory=list)


class StorageCfg(Strict):
    type: Literal["duckdb", "jsonl"] = "duckdb"
    table: str = "samples"
    export_jsonl: Optional[str] = None


class OutputCfg(Strict):
    path: str
    format: Optional[str] = None
    multi_turn: bool = False
    thinking: bool = False
    resume: bool = False
    cache_cleanup: bool = True
    storage: StorageCfg = Field(default_factory=StorageCfg)


class Config(Strict):
    run: RunCfg = Field(default_factory=RunCfg)
    llm: LlmCfg = Field(default_factory=LlmCfg)
    embedding: EmbeddingCfg = Field(default_factory=EmbeddingCfg)
    endpoints: Dict[str, LlmCfg] = Field(default_factory=dict)
    plan: PlanCfg = Field(default_factory=PlanCfg)
    strategies: List[StrategyCfg] = Field(default_factory=list)
    pipeline: List[StageCfg] = Field(default_factory=list)
    judge: JudgeCfg = Field(default_factory=JudgeCfg)
    output: Optional[OutputCfg] = None
