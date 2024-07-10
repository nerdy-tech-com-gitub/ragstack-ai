import json
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    cast,
)

from cassandra.cluster import ConsistencyLevel, Session
from cassio.config import check_resolve_keyspace, check_resolve_session

from ._mmr_helper import MmrHelper
from .concurrency import ConcurrentQueries
from .content import Kind
from .embedding_model import EmbeddingModel
from .links import Link

CONTENT_ID = "content_id"


@dataclass
class Node:
    """Node in the GraphStore."""

    text: str
    """Text contained by the node."""
    id: Optional[str] = None
    """Unique ID for the node. Will be generated by the GraphStore if not set."""
    metadata: Dict[str, Any] = field(default_factory=dict)
    """Metadata for the node."""
    links: Set[Link] = field(default_factory=set)
    """Links for the node."""


class SetupMode(Enum):
    """Mode used to create the Cassandra table."""

    SYNC = 1
    ASYNC = 2
    OFF = 3


def _serialize_metadata(md: Dict[str, Any]) -> str:
    if isinstance(md.get("links"), Set):
        md = md.copy()
        md["links"] = list(md["links"])
    return json.dumps(md)


def _serialize_links(links: Set[Link]) -> str:
    import dataclasses

    class SetAndLinkEncoder(json.JSONEncoder):
        def default(self, obj: Any) -> Any:
            if dataclasses.is_dataclass(obj):
                return dataclasses.asdict(obj)

            try:
                iterable = iter(obj)
            except TypeError:
                pass
            else:
                return list(iterable)
            # Let the base class default method raise the TypeError
            return super().default(obj)

    return json.dumps(list(links), cls=SetAndLinkEncoder)


def _deserialize_metadata(json_blob: Optional[str]) -> Dict[str, Any]:
    # We don't need to convert the links list back to a set -- it will be
    # converted when accessed, if needed.
    return cast(Dict[str, Any], json.loads(json_blob or ""))


def _deserialize_links(json_blob: Optional[str]) -> Set[Link]:
    return {
        Link(kind=link["kind"], direction=link["direction"], tag=link["tag"])
        for link in cast(List[Dict[str, Any]], json.loads(json_blob or ""))
    }


def _row_to_node(row: Any) -> Node:
    metadata = _deserialize_metadata(row.metadata_blob)
    links = _deserialize_links(row.links_blob)
    return Node(
        id=row.content_id,
        text=row.text_content,
        metadata=metadata,
        links=links,
    )


@dataclass
class _Edge:
    target_content_id: str
    target_text_embedding: List[float]


class GraphStore:
    """A hybrid vector-and-graph store backed by Cassandra.

    Document chunks support vector-similarity search as well as edges linking
    documents based on structural and semantic properties.

    Args:
        embedding: The embeddings to use for the document content.
        setup_mode: Mode used to create the Cassandra table (SYNC,
            ASYNC or OFF).
    """

    def __init__(
        self,
        embedding: EmbeddingModel,
        *,
        node_table: str = "graph_nodes",
        targets_table: str = "graph_targets",
        session: Optional[Session] = None,
        keyspace: Optional[str] = None,
        setup_mode: SetupMode = SetupMode.SYNC,
    ):
        session = check_resolve_session(session)
        keyspace = check_resolve_keyspace(keyspace)

        self._embedding = embedding
        self._node_table = node_table
        self._targets_table = targets_table
        self._session = session
        self._keyspace = keyspace

        if setup_mode == SetupMode.SYNC:
            self._apply_schema()
        elif setup_mode != SetupMode.OFF:
            raise ValueError(
                f"Invalid setup mode {setup_mode.name}. "
                "Only SYNC and OFF are supported at the moment"
            )

        # TODO: Parent ID / source ID / etc.
        self._insert_passage = session.prepare(
            f"""
            INSERT INTO {keyspace}.{node_table} (
                content_id, kind, text_content, text_embedding, link_to_tags,
                metadata_blob, links_blob
            ) VALUES (?, '{Kind.passage}', ?, ?, ?, ?, ?)
            """
        )

        self._insert_tag = session.prepare(
            f"""
            INSERT INTO {keyspace}.{targets_table} (
                target_content_id, kind, tag, target_text_embedding
            ) VALUES (?, ?, ?, ?)
            """
        )

        self._query_by_id = session.prepare(
            f"""
            SELECT content_id, kind, text_content, metadata_blob, links_blob
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """
        )

        self._query_by_embedding = session.prepare(
            f"""
            SELECT content_id, kind, text_content, metadata_blob, links_blob
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_by_embedding.consistency_level = ConsistencyLevel.ONE

        self._query_ids_and_link_to_tags_by_embedding = session.prepare(
            f"""
            SELECT content_id, link_to_tags
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_ids_and_link_to_tags_by_embedding.consistency_level = (
            ConsistencyLevel.ONE
        )

        self._query_ids_and_link_to_tags_by_id = session.prepare(
            f"""
            SELECT content_id, link_to_tags
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """
        )

        self._query_ids_and_embedding_by_embedding = session.prepare(
            f"""
            SELECT content_id, text_embedding
            FROM {keyspace}.{node_table}
            ORDER BY text_embedding ANN OF ?
            LIMIT ?
            """
        )
        self._query_ids_and_embedding_by_embedding.consistency_level = (
            ConsistencyLevel.ONE
        )

        self._query_source_tags_by_id = session.prepare(
            f"""
            SELECT link_to_tags
            FROM {keyspace}.{node_table}
            WHERE content_id = ?
            """
        )

        self._query_targets_embeddings_by_kind_and_tag_and_embedding = session.prepare(
            f"""
            SELECT target_content_id, target_text_embedding, tag
            FROM {keyspace}.{targets_table}
            WHERE kind = ? AND tag = ?
            ORDER BY target_text_embedding ANN of ?
            LIMIT ?
            """
        )

        self._query_targets_by_kind_and_value = session.prepare(
            f"""
            SELECT target_content_id, kind, tag
            FROM {keyspace}.{targets_table}
            WHERE kind = ? AND tag = ?
            """
        )

    def _apply_schema(self) -> None:
        """Apply the schema to the database."""
        embedding_dim = len(self._embedding.embed_query("Test Query"))
        self._session.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._keyspace}.{self._node_table} (
                content_id TEXT,
                kind TEXT,
                text_content TEXT,
                text_embedding VECTOR<FLOAT, {embedding_dim}>,

                link_to_tags SET<TUPLE<TEXT, TEXT>>,
                metadata_blob TEXT,
                links_blob TEXT,

                PRIMARY KEY (content_id)
            )
            """
        )

        self._session.execute(
            f"""CREATE TABLE IF NOT EXISTS {self._keyspace}.{self._targets_table} (
                target_content_id TEXT,
                kind TEXT,
                tag TEXT,

                -- text_embedding of target node. allows MMR to be applied without
                -- fetching nodes.
                target_text_embedding VECTOR<FLOAT, {embedding_dim}>,

                PRIMARY KEY ((kind, tag), target_content_id)
            )
            """
        )

        # Index on text_embedding (for similarity search)
        self._session.execute(f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._node_table}_text_embedding_index
            ON {self._keyspace}.{self._node_table}(text_embedding)
            USING 'StorageAttachedIndex';
        """)

        # Index on target_text_embedding (for similarity search)
        self._session.execute(f"""
            CREATE CUSTOM INDEX IF NOT EXISTS {self._targets_table}_target_text_embedding_index
            ON {self._keyspace}.{self._targets_table}(target_text_embedding)
            USING 'StorageAttachedIndex';
        """)  # noqa: E501

    def _concurrent_queries(self) -> ConcurrentQueries:
        return ConcurrentQueries(self._session)

    # TODO: Async (aadd_nodes)
    def add_nodes(
        self,
        nodes: Iterable[Node],
    ) -> Iterable[str]:
        """Add nodes to the graph store."""
        node_ids: List[str] = []
        texts: List[str] = []
        metadatas: List[Dict[str, Any]] = []
        nodes_links: List[Set[Link]] = []
        for node in nodes:
            if not node.id:
                node_ids.append(secrets.token_hex(8))
            else:
                node_ids.append(node.id)
            texts.append(node.text)
            metadatas.append(node.metadata)
            nodes_links.append(node.links)

        text_embeddings = self._embedding.embed_texts(texts)

        with self._concurrent_queries() as cq:
            tuples = zip(node_ids, texts, text_embeddings, metadatas, nodes_links)
            for node_id, text, text_embedding, metadata, links in tuples:
                link_to_tags = set()  # link to these tags
                link_from_tags = set()  # link from these tags

                for tag in links:
                    if tag.direction == "in" or tag.direction == "bidir":
                        # An incoming link should be linked *from* nodes with the given
                        # tag.
                        link_from_tags.add((tag.kind, tag.tag))
                    if tag.direction == "out" or tag.direction == "bidir":
                        link_to_tags.add((tag.kind, tag.tag))

                metadata_blob = _serialize_metadata(metadata)
                links_blob = _serialize_links(links)
                cq.execute(
                    self._insert_passage,
                    parameters=(
                        node_id,
                        text,
                        text_embedding,
                        link_to_tags,
                        metadata_blob,
                        links_blob,
                    ),
                )

                for kind, value in link_from_tags:
                    cq.execute(
                        self._insert_tag,
                        parameters=(node_id, kind, value, text_embedding),
                    )

        return node_ids

    def _nodes_with_ids(
        self,
        ids: Iterable[str],
    ) -> List[Node]:
        results: Dict[str, Optional[Node]] = {}
        with self._concurrent_queries() as cq:

            def add_nodes(rows: Iterable[Any]) -> None:
                # Should always be exactly one row here. We don't need to check
                #   1. The query is for a `ID == ?` query on the primary key.
                #   2. If it doesn't exist, the `get_result` method below will
                #      raise an exception indicating the ID doesn't exist.
                for row in rows:
                    results[row.content_id] = _row_to_node(row)

            for node_id in ids:
                if node_id not in results:
                    # Mark this node ID as being fetched.
                    results[node_id] = None
                    cq.execute(
                        self._query_by_id, parameters=(node_id,), callback=add_nodes
                    )

        def get_result(node_id: str) -> Node:
            if (result := results[node_id]) is None:
                raise ValueError(f"No node with ID '{node_id}'")
            return result

        return [get_result(node_id) for node_id in ids]

    def mmr_traversal_search(
        self,
        query: str,
        *,
        k: int = 4,
        depth: int = 2,
        fetch_k: int = 100,
        adjacent_k: int = 10,
        lambda_mult: float = 0.5,
        score_threshold: float = float("-inf"),
    ) -> Iterable[Node]:
        """Retrieve documents from this graph store using MMR-traversal.

        This strategy first retrieves the top `fetch_k` results by similarity to
        the question. It then selects the top `k` results based on
        maximum-marginal relevance using the given `lambda_mult`.

        At each step, it considers the (remaining) documents from `fetch_k` as
        well as any documents connected by edges to a selected document
        retrieved based on similarity (a "root").

        Args:
            query: The query string to search for.
            k: Number of Documents to return. Defaults to 4.
            fetch_k: Number of initial Documents to fetch via similarity.
                Defaults to 100.
            adjacent_k: Number of adjacent Documents to fetch.
                Defaults to 10.
            depth: Maximum depth of a node (number of edges) from a node
                retrieved via similarity. Defaults to 2.
            lambda_mult: Number between 0 and 1 that determines the degree
                of diversity among the results with 0 corresponding to maximum
                diversity and 1 to minimum diversity. Defaults to 0.5.
            score_threshold: Only documents with a score greater than or equal
                this threshold will be chosen. Defaults to -infinity.
        """
        query_embedding = self._embedding.embed_query(query)
        helper = MmrHelper(
            k=k,
            query_embedding=query_embedding,
            lambda_mult=lambda_mult,
            score_threshold=score_threshold,
        )

        # Fetch the initial candidates and add them to the helper.
        fetched = self._session.execute(
            self._query_ids_and_embedding_by_embedding,
            (query_embedding, fetch_k),
        )
        helper.add_candidates({row.content_id: row.text_embedding for row in fetched})

        # Select the best item, K times.
        depths = {candidate_id: 0 for candidate_id in helper.candidate_ids()}
        visited_tags: Set[Tuple[str, str]] = set()
        for _ in range(k):
            selected_id = helper.pop_best()

            if selected_id is None:
                break

            next_depth = depths[selected_id] + 1
            if next_depth < depth:
                # If the next nodes would not exceed the depth limit, find the
                # adjacent nodes.
                #
                # TODO: For a big performance win, we should track which tags we've
                # already incorporated. We don't need to issue adjacent queries for
                # those.
                adjacents = self._get_adjacent(
                    [selected_id],
                    visited_tags=visited_tags,
                    query_embedding=query_embedding,
                    k_per_tag=adjacent_k,
                )

                new_candidates = {}
                for adjacent in adjacents:
                    new_candidates[adjacent.target_content_id] = (
                        adjacent.target_text_embedding
                    )
                    if next_depth < depths.get(adjacent.target_content_id, depth + 1):
                        # If this is a new shortest depth, or there was no
                        # previous depth, update the depths. This ensures that
                        # when we discover a node we will have the shortest
                        # depth available.
                        #
                        # NOTE: No effort is made to traverse from nodes that
                        # were previously selected if they become reachable via
                        # a shorter path via nodes selected later. This is
                        # currently "intended", but may be worth experimenting
                        # with.
                        depths[adjacent.target_content_id] = next_depth
                helper.add_candidates(new_candidates)

        return self._nodes_with_ids(helper.selected_ids)

    def traversal_search(
        self, query: str, *, k: int = 4, depth: int = 1
    ) -> Iterable[Node]:
        """Retrieve documents from this knowledge store.

        First, `k` nodes are retrieved using a vector search for the `query` string.
        Then, additional nodes are discovered up to the given `depth` from those
        starting nodes.

        Args:
            query: The query string.
            k: The number of Documents to return from the initial vector search.
                Defaults to 4.
            depth: The maximum depth of edges to traverse. Defaults to 1.

        Returns:
            Collection of retrieved documents.
        """
        # Depth 0:
        #   Query for `k` nodes similar to the question.
        #   Retrieve `content_id` and `link_to_tags`.
        #
        # Depth 1:
        #   Query for nodes that have an incoming tag in the `link_to_tags` set.
        #   Combine node IDs.
        #   Query for `link_to_tags` of those "new" node IDs.
        #
        # ...

        with self._concurrent_queries() as cq:
            # Map from visited ID to depth
            visited_ids: Dict[str, int] = {}

            # Map from visited tag `(kind, tag)` to depth. Allows skipping queries
            # for tags that we've already traversed.
            visited_tags: Dict[Tuple[str, str], int] = {}

            def visit_nodes(d: int, nodes: Sequence[Any]) -> None:
                nonlocal visited_ids
                nonlocal visited_tags

                # Visit nodes at the given depth.
                # Each node has `content_id` and `link_to_tags`.

                # Iterate over nodes, tracking the *new* outgoing kind tags for this
                # depth. This is tags that are either new, or newly discovered at a
                # lower depth.
                outgoing_tags = set()
                for node in nodes:
                    content_id = node.content_id

                    # Add visited ID. If it is closer it is a new node at this depth:
                    if d <= visited_ids.get(content_id, depth):
                        visited_ids[content_id] = d

                        # If we can continue traversing from this node,
                        if d < depth and node.link_to_tags:
                            # Record any new (or newly discovered at a lower depth)
                            # tags to the set to traverse.
                            for kind, value in node.link_to_tags:
                                if d <= visited_tags.get((kind, value), depth):
                                    # Record that we'll query this tag at the
                                    # given depth, so we don't fetch it again
                                    # (unless we find it an earlier depth)
                                    visited_tags[(kind, value)] = d
                                    outgoing_tags.add((kind, value))

                if outgoing_tags:
                    # If there are new tags to visit at the next depth, query for the
                    # node IDs.
                    for kind, value in outgoing_tags:
                        cq.execute(
                            self._query_targets_by_kind_and_value,
                            parameters=(
                                kind,
                                value,
                            ),
                            callback=lambda rows, d=d: visit_targets(d, rows),
                        )

            def visit_targets(d: int, targets: Sequence[Any]) -> None:
                nonlocal visited_ids

                # target_content_id, tag=(kind,value)
                new_nodes_at_next_depth = set()
                for target in targets:
                    content_id = target.target_content_id
                    if d < visited_ids.get(content_id, depth):
                        new_nodes_at_next_depth.add(content_id)

                if new_nodes_at_next_depth:
                    for node_id in new_nodes_at_next_depth:
                        cq.execute(
                            self._query_ids_and_link_to_tags_by_id,
                            parameters=(node_id,),
                            callback=lambda rows, d=d: visit_nodes(d + 1, rows),
                        )

            query_embedding = self._embedding.embed_query(query)
            cq.execute(
                self._query_ids_and_link_to_tags_by_embedding,
                parameters=(query_embedding, k),
                callback=lambda nodes: visit_nodes(0, nodes),
            )

        return self._nodes_with_ids(visited_ids.keys())

    def similarity_search(
        self,
        embedding: List[float],
        k: int = 4,
    ) -> Iterable[Node]:
        """Retrieve nodes similar to the given embedding."""
        for row in self._session.execute(self._query_by_embedding, (embedding, k)):
            yield _row_to_node(row)

    def _get_adjacent(
        self,
        source_ids: Iterable[str],
        visited_tags: Set[Tuple[str, str]],
        query_embedding: List[float],
        k_per_tag: Optional[int] = None,
    ) -> Iterable[_Edge]:
        """Return the target nodes adjacent to any of the source nodes.

        Args:
            source_ids: The source IDs to start from when retrieving adjacent nodes.
            visited_tags: Tags we've already visited.
            query_embedding: The query embedding. Used to rank target nodes.
            k_per_tag: The number of target nodes to fetch for each outgoing tag.

        Returns:
            List of adjacent edges.
        """
        targets: Dict[str, List[float]] = {}

        def add_sources(rows: Iterable[Any]) -> None:
            for row in rows:
                for new_tag in row.link_to_tags or []:
                    if new_tag not in visited_tags:
                        visited_tags.add(new_tag)

                        cq.execute(
                            self._query_targets_embeddings_by_kind_and_tag_and_embedding,
                            parameters=(
                                new_tag[0],
                                new_tag[1],
                                query_embedding,
                                k_per_tag or 10,
                            ),
                            callback=add_targets,
                        )

        def add_targets(rows: Iterable[Any]) -> None:
            # TODO: Figure out how to use the "kind" on the edge.
            # This is tricky, since we currently issue one query for anything
            # adjacent via any kind, and we don't have enough information to
            # determine which kind(s) a given target was reached from.
            for row in rows:
                targets.setdefault(row.target_content_id, row.target_text_embedding)

        with self._concurrent_queries() as cq:
            # TODO: We could eliminate this query by storing the source tags of the
            # target node in the targets table.
            for source_id in source_ids:
                cq.execute(
                    self._query_source_tags_by_id, (source_id,), callback=add_sources
                )

        # TODO: Consider a combined limit based on the similarity and/or predicated MMR score?  # noqa: E501
        return [
            _Edge(target_content_id=content_id, target_text_embedding=embedding)
            for (content_id, embedding) in targets.items()
        ]
