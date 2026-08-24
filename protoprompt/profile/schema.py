{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "properties": {
    "traits": {
      "type": "object",
      "properties": {
        "style": { "type": "string" },
        "expertise": { "type": "string", "enum": ["beginner", "intermediate", "expert"] },
        "verbosity": { "type": "string", "enum": ["concise", "balanced", "detailed"] },
        "formality": { "type": "string", "enum": ["casual", "neutral", "formal"] }
      }
    },
    "preferences": {
      "type": "object",
      "properties": {
        "format": { "type": "string", "enum": ["bullets", "narrative", "code_heavy", "mixed"] },
        "language": { "type": "string" },
        "topics": { "type": "array", "items": { "type": "string" } }
      }
    },
    "summary": { "type": "string" }
  }
}
