import pytest
import json
import os
import time
from unittest.mock import MagicMock, patch, mock_open
from datetime import datetime, timedelta, timezone

from backend.models import Issue, EscalationAudit, EscalationReason, Grievance
from backend.adaptive_weights import AdaptiveWeights
from backend.trend_analyzer import TrendAnalyzer
from backend.civic_intelligence import CivicIntelligenceEngine
from backend.spatial_utils import get_cluster_representative

# Mock data
MOCK_WEIGHTS = {
    "severity_keywords": {"critical": ["fire"]},
    "urgency_patterns": [],
    "category_keywords": {"Fire": ["fire"]},
    "category_multipliers": {"Fire": 1.0},
    "duplicate_search_radius": 50.0
}

@pytest.fixture
def mock_adaptive_weights():
    with patch('backend.adaptive_weights.DATA_FILE', 'mock_weights.json'):
        with patch('builtins.open', mock_open(read_data=json.dumps(MOCK_WEIGHTS))) as m:
            with patch('os.path.exists', return_value=True):
                with patch('os.path.getmtime', return_value=100):
                    # Reset singleton
                    AdaptiveWeights._instance = None
                    weights = AdaptiveWeights()
                    yield weights
                    AdaptiveWeights._instance = None

def test_adaptive_weights_load(mock_adaptive_weights):
    assert mock_adaptive_weights.get_category_multipliers()["Fire"] == 1.0
    assert mock_adaptive_weights.get_severity_keywords()["critical"] == ["fire"]

def test_adaptive_weights_update_category(mock_adaptive_weights):
    with patch('builtins.open', mock_open(read_data=json.dumps(MOCK_WEIGHTS))) as m:
        # We need to mock getmtime to allow save to proceed without reload override
        with patch('os.path.getmtime', side_effect=[100, 200, 200, 200, 200]):
            mock_adaptive_weights.update_category_weight("Fire", 1.5)

            assert mock_adaptive_weights.get_category_multipliers()["Fire"] == 1.5
            # Verify file write
            m().write.assert_called()

def test_trend_analyzer_keywords():
    analyzer = TrendAnalyzer()
    issues = [
        Issue(description="Fire in the building help"),
        Issue(description="Big fire burning here"),
        Issue(description="Building has a fire problem")
    ]

    result = analyzer.analyze(issues)
    keywords = dict(result['top_keywords'])

    # "fire" should be top
    assert "fire" in keywords
    assert keywords["fire"] == 3
    # "building" should be there
    assert "building" in keywords
    assert keywords["building"] == 2

def test_trend_analyzer_categories():
    analyzer = TrendAnalyzer()
    issues = [
        Issue(category="Fire"),
        Issue(category="Fire"),
        Issue(category="Water")
    ]

    result = analyzer.analyze(issues)
    dist = result['category_distribution']

    assert dist["Fire"] == 2
    assert dist["Water"] == 1

@patch('backend.trend_analyzer.cluster_issues_dbscan')
@patch('backend.trend_analyzer.get_cluster_representative')
def test_trend_analyzer_clusters(mock_get_rep, mock_dbscan):
    analyzer = TrendAnalyzer()

    # Mock cluster result: 2 clusters, one with 3 items, one with 2
    cluster1 = [MagicMock(), MagicMock(), MagicMock()]
    cluster2 = [MagicMock(), MagicMock()]

    mock_dbscan.return_value = [cluster1, cluster2]

    # Mock representative
    mock_rep = MagicMock()
    mock_rep.latitude = 10.0
    mock_rep.longitude = 20.0
    mock_rep.category = "Test"
    mock_rep.description = "Test desc"
    mock_get_rep.return_value = mock_rep

    mock_issue = MagicMock()
    mock_issue.description = "test"
    result = analyzer.analyze([mock_issue]) # Input doesn't matter as we mock dbscan

    clusters = result['clusters']
    assert len(clusters) == 1 # Only cluster1 (size 3) should be returned, cluster2 (size 2) filtered out
    assert clusters[0]['count'] == 3
    assert clusters[0]['latitude'] == 10.0

@patch('backend.civic_intelligence.SessionLocal')
@patch('backend.civic_intelligence.trend_analyzer')
@patch('backend.civic_intelligence.adaptive_weights')
@patch('builtins.open', new_callable=mock_open)
@patch('json.dump')
def test_civic_intelligence_run(mock_json_dump, mock_file_open, mock_weights, mock_trend_analyzer, mock_db_session):
    engine = CivicIntelligenceEngine()

    # Mock DB
    mock_session = MagicMock()
    mock_db_session.return_value = mock_session

    # We need to construct the chain of calls properly
    # db.query(Issue).filter(...).all()

    # Create mock query objects
    mock_query_issues = MagicMock()
    mock_query_upgrades = MagicMock()
    mock_query_grievance = MagicMock()
    mock_query_resolved = MagicMock()

    # Define query side effects
    def query_side_effect(model):
        if model == Issue:
            return mock_query_issues
        elif model == EscalationAudit:
            return mock_query_upgrades
        elif model == Grievance:
            return mock_query_grievance
        return MagicMock()

    mock_session.query.side_effect = query_side_effect

    # Setup results
    issues_result = [Issue(id=1, resolved_at=None), Issue(id=2, resolved_at=datetime.now(timezone.utc))]

    # Issue Query Chain
    mock_query_issues.filter.return_value.all.return_value = issues_result # issues_24h
    mock_query_issues.filter.return_value.count.return_value = 1 # resolved_count

    # Upgrade Query Chain
    mock_query_upgrades.filter.return_value.all.return_value = [] # No upgrades

    # Setup Trend Analyzer
    mock_trend_analyzer.analyze.return_value = {
        "top_keywords": [],
        "category_distribution": {},
        "clusters": []
    }

    # Setup Adaptive Weights
    mock_weights.get_duplicate_search_radius.return_value = 50.0

    # Run
    engine.run_daily_cycle()

    # Verify trend analyzer called
    mock_trend_analyzer.analyze.assert_called()

    # Verify snapshot saved
    mock_file_open.assert_called()
    mock_json_dump.assert_called()
