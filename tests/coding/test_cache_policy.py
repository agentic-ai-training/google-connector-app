from app.coding.cache_policy import select_cache_policy


def test_cache_policy_fails_closed_for_unknown_and_sensitive_entities():
    assert select_cache_policy("unregistered").cacheable is False
    assert select_cache_policy("secret").cacheable is False
    assert select_cache_policy("raw_private_content").cacheable is False
    assert select_cache_policy("write_result").may_satisfy_live_postcondition is False


def test_cache_policy_is_entity_specific_and_never_live_write_proof():
    immutable = select_cache_policy("immutable_source")
    health = select_cache_policy("process_health")
    schema = select_cache_policy("database_schema")
    assert immutable.ttl_seconds > schema.ttl_seconds > health.ttl_seconds
    assert immutable.invalidation == "content_hash_changed"
    assert health.may_satisfy_live_postcondition is False
