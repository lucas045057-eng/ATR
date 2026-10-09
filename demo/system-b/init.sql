CREATE TABLE inventory_items (
  item_key VARCHAR(40) PRIMARY KEY,
  display_name VARCHAR(120) NOT NULL
);
INSERT INTO inventory_items(item_key, display_name) VALUES ('demo-1', 'Read-only sample item');
