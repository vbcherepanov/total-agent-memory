CREATE TABLE representation_index_revision (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    revision INTEGER NOT NULL DEFAULT 0
);
INSERT INTO representation_index_revision(singleton,revision) VALUES(1,0);
CREATE TRIGGER repr_revision_insert AFTER INSERT ON knowledge_representations
BEGIN UPDATE representation_index_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER repr_revision_update AFTER UPDATE ON knowledge_representations
BEGIN UPDATE representation_index_revision SET revision=revision+1 WHERE singleton=1; END;
CREATE TRIGGER repr_revision_delete AFTER DELETE ON knowledge_representations
BEGIN UPDATE representation_index_revision SET revision=revision+1 WHERE singleton=1; END;
