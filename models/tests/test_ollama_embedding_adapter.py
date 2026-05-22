"""
Tests for models/ollama_embedding_adapter.py.

All HTTP interactions are mocked with unittest.mock.patch — no live Ollama
instance is required or used.
"""
from unittest.mock import MagicMock, patch

import pytest

from memory.artifact_governance import EMBEDDING_VISIBLE_FIELDS_VERSION
from models.embedding_adapter import EmbeddingAdapter
from models.ollama_embedding_adapter import (
    OLLAMA_EMBEDDING_ADAPTER_NAME,
    OLLAMA_EMBEDDING_DEFAULT_BASE_URL,
    VERSION,
    OllamaEmbeddingAdapter,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DIMS = 4
MODEL = 'nomic-embed-text'
DIGEST = 'sha256:abc123def456789'
VECTOR = [0.1, 0.2, 0.3, 0.4]


def _make_show_response(digest: str = DIGEST) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {'digest': digest}
    return resp


def _make_embed_response(vector=None) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {'embedding': vector or VECTOR}
    return resp


def _adapter(**kwargs) -> OllamaEmbeddingAdapter:
    defaults = {'model_name': MODEL, 'expected_dimensions': DIMS}
    defaults.update(kwargs)
    return OllamaEmbeddingAdapter(**defaults)


# ---------------------------------------------------------------------------
# ABC conformance
# ---------------------------------------------------------------------------

class TestABCConformance:
    def test_is_embedding_adapter_subclass(self):
        assert issubclass(OllamaEmbeddingAdapter, EmbeddingAdapter)

    def test_instantiates_without_requests(self):
        # Import must succeed even when requests is not imported yet.
        adapter = _adapter()
        assert adapter is not None

    def test_adapter_name(self):
        assert _adapter().adapter_name == OLLAMA_EMBEDDING_ADAPTER_NAME

    def test_adapter_version(self):
        assert _adapter().adapter_version == VERSION

    def test_model_name(self):
        assert _adapter().model_name == MODEL

    def test_model_version_equals_model_name(self):
        assert _adapter().model_version == MODEL

    def test_provider_name(self):
        assert _adapter().provider_name == 'ollama'

    def test_dimensions(self):
        assert _adapter().dimensions == DIMS

    def test_model_digest_is_none_before_embed(self):
        assert _adapter().model_digest is None

    def test_default_base_url(self):
        adapter = _adapter()
        assert adapter._base_url == OLLAMA_EMBEDDING_DEFAULT_BASE_URL.rstrip('/')


# ---------------------------------------------------------------------------
# embed()
# ---------------------------------------------------------------------------

class TestEmbed:
    def test_returns_vector(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            result = adapter.embed('hello world')
        assert result == VECTOR

    def test_loads_digest_on_first_embed(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(DIGEST), _make_embed_response()]
            adapter.embed('hello')
        assert adapter.model_digest == DIGEST

    def test_digest_loaded_only_once(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [
                _make_show_response(),
                _make_embed_response(),
                _make_embed_response(),
            ]
            adapter.embed('first')
            adapter.embed('second')
        # 3 calls: /api/show once + /api/embeddings twice
        assert mock_post.call_count == 3

    def test_calls_embeddings_endpoint(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            adapter.embed('test')
        embed_call = mock_post.call_args_list[1]
        assert '/api/embeddings' in embed_call[0][0]

    def test_calls_show_endpoint_for_digest(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            adapter.embed('test')
        show_call = mock_post.call_args_list[0]
        assert '/api/show' in show_call[0][0]

    def test_raises_on_dimension_mismatch(self):
        adapter = OllamaEmbeddingAdapter(MODEL, expected_dimensions=8)
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response([0.1, 0.2])]
            with pytest.raises(ValueError, match='dimensions'):
                adapter.embed('test')

    def test_raises_on_http_error(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            err_resp = MagicMock()
            err_resp.raise_for_status.side_effect = Exception('HTTP 500')
            mock_post.return_value = err_resp
            with pytest.raises(Exception, match='HTTP 500'):
                adapter.embed('test')


# ---------------------------------------------------------------------------
# producer_version
# ---------------------------------------------------------------------------

class TestProducerVersion:
    def test_before_embed_no_digest(self):
        pv = _adapter().producer_version
        assert pv == f'{VERSION}:no-digest'

    def test_after_embed_includes_digest_prefix(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(DIGEST), _make_embed_response()]
            adapter.embed('test')
        assert adapter.producer_version == f'{VERSION}:{DIGEST[:12]}'

    def test_producer_version_stable_across_calls(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [
                _make_show_response(DIGEST),
                _make_embed_response(),
                _make_embed_response(),
            ]
            adapter.embed('first')
            adapter.embed('second')
        assert adapter.producer_version == f'{VERSION}:{DIGEST[:12]}'


# ---------------------------------------------------------------------------
# get_provenance()
# ---------------------------------------------------------------------------

class TestGetProvenance:
    def test_provenance_keys_present(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            adapter.embed('test')
        prov = adapter.get_provenance()
        required = {
            'adapter_name', 'adapter_version', 'model_name', 'model_version',
            'model_digest', 'provider_name', 'dimensions', 'producer_version',
            'embedding_visible_fields_version',
        }
        assert required <= set(prov.keys())

    def test_embedding_visible_fields_version_is_correct(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            adapter.embed('test')
        prov = adapter.get_provenance()
        assert prov['embedding_visible_fields_version'] == EMBEDDING_VISIBLE_FIELDS_VERSION

    def test_provenance_reflects_loaded_digest(self):
        adapter = _adapter()
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(DIGEST), _make_embed_response()]
            adapter.embed('test')
        prov = adapter.get_provenance()
        assert prov['model_digest'] == DIGEST

    def test_provenance_before_embed_has_none_digest(self):
        prov = _adapter().get_provenance()
        assert prov['model_digest'] is None


# ---------------------------------------------------------------------------
# base_url handling
# ---------------------------------------------------------------------------

class TestBaseUrl:
    def test_trailing_slash_stripped(self):
        adapter = OllamaEmbeddingAdapter(MODEL, expected_dimensions=DIMS,
                                          base_url='http://localhost:11434/')
        assert not adapter._base_url.endswith('/')

    def test_custom_base_url_used_in_requests(self):
        adapter = OllamaEmbeddingAdapter(MODEL, expected_dimensions=DIMS,
                                          base_url='http://custom:9999')
        with patch('requests.post') as mock_post:
            mock_post.side_effect = [_make_show_response(), _make_embed_response()]
            adapter.embed('test')
        urls = [call[0][0] for call in mock_post.call_args_list]
        assert all('custom:9999' in u for u in urls)
