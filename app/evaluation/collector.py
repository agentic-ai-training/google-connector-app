async def record_run_evaluation(pool, run_id) -> None:
    """Persist multi-objective terminal facts without inventing human judgments."""
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO workflow_evaluations
               (run_id,suite_version,policy_name,policy_version,candidate,
                task_success,plan_correctness,tool_correctness,artifact_correctness,
                recovery_success,side_effect_integrity,user_satisfaction,
                retrieval_quality,latency_ms,tokens,evidence)
               SELECT r.id,'production-facts-v1','durable-executor',
                      coalesce(r.deployment_version,'unknown'),FALSE,
                      CASE WHEN r.status='completed' THEN 1 ELSE 0 END,
                      CASE WHEN jsonb_array_length(coalesce(r.plan->'steps','[]'))>0
                           THEN 1 ELSE 0 END,
                      (SELECT CASE WHEN count(*)=0 THEN NULL ELSE
                         count(*) FILTER(WHERE status='succeeded')::numeric/count(*) END
                       FROM agent_tool_attempts t WHERE t.run_id=r.id),
                      (SELECT CASE WHEN count(*)=0 THEN NULL ELSE
                         count(*) FILTER(WHERE verification_status='verified')::numeric/count(*) END
                       FROM agent_artifacts a WHERE a.run_id=r.id),
                      (SELECT CASE WHEN count(*)=0 THEN NULL
                         WHEN r.status='completed' THEN 1 ELSE 0 END
                       FROM agent_run_events e WHERE e.run_id=r.id
                         AND e.event_type='retry_scheduled'),
                      r.side_effect_integrity/100.0,
                      (SELECT avg(CASE WHEN rating=1 THEN 1 WHEN rating=-1 THEN 0
                                       ELSE 0.5 END) FROM feedback f WHERE f.run_id=r.id),
                      (SELECT avg(CASE WHEN returned_count=0 THEN NULL ELSE
                         used_count::numeric/returned_count END)
                       FROM rag_retrieval_events q WHERE q.run_id=r.id),
                      (extract(epoch FROM (r.completed_at-r.started_at))*1000)::bigint,
                      r.input_tokens+r.output_tokens,
                      jsonb_build_object('status',r.status,'error_category',r.error_category,
                                         'completion',jsonb_build_object(
                                           'technical',r.technical_completion,
                                           'functional',r.functional_completion,
                                           'user_visible',r.user_visible_completion,
                                           'side_effect_integrity',r.side_effect_integrity))
               FROM agent_runs r WHERE r.id=$1 AND r.completed_at IS NOT NULL
               ON CONFLICT(run_id,policy_name,policy_version) DO UPDATE SET
                 task_success=EXCLUDED.task_success,
                 plan_correctness=EXCLUDED.plan_correctness,
                 tool_correctness=EXCLUDED.tool_correctness,
                 artifact_correctness=EXCLUDED.artifact_correctness,
                 recovery_success=EXCLUDED.recovery_success,
                 side_effect_integrity=EXCLUDED.side_effect_integrity,
                 user_satisfaction=EXCLUDED.user_satisfaction,
                 retrieval_quality=EXCLUDED.retrieval_quality,
                 latency_ms=EXCLUDED.latency_ms,tokens=EXCLUDED.tokens,
                 evidence=EXCLUDED.evidence,created_at=now()""",
            run_id,
        )
