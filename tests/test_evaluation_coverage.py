from ml_tools.evaluate import summarize


def test_abstentions_do_not_hide_positive_videos_without_a_result():
    rows = [
        dict(status='done', label='violation', verdict='violation', elapsed_sec=1, event_times=[3]),
        dict(status='done', label='violation', verdict='no_violation', elapsed_sec=1, event_times=[]),
        dict(status='failed', label='violation', error_code='insufficient_evidence', elapsed_sec=1),
        dict(status='failed', label='no_violation', error_code='local_analysis_failed', elapsed_sec=1),
        dict(status='done', label='no_violation', verdict='no_violation', elapsed_sec=1, event_times=[]),
    ]
    result = summarize(rows)
    assert result['recall_completed'] == 0.5
    assert result['recall_over_all_positives'] == 0.3333
    assert result['positive_videos_without_supported_result'] == 2
    assert result['abstained'] == 1
    assert result['technical_failed'] == 1
    assert result['coverage'] == 0.6


def test_legacy_failures_are_not_guessed_to_be_abstentions_or_technical_errors():
    result = summarize([dict(status='failed', label='violation', elapsed_sec=1, error='old free text')])
    assert result['failed_unclassified'] == 1
    assert result['abstained'] == result['technical_failed'] == 0
    assert result['recall_completed'] is None
    assert result['recall_over_all_positives'] == 0
