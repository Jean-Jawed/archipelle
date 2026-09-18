-- Échecs d'extraction permanents (mot de passe, fichier endommagé), mémorisés pour ne pas
-- ré-analyser le fichier à chaque recherche. Effacés quand le fichier change (nouvelle entrée).

ALTER TABLE documents ADD COLUMN failure TEXT;  -- JSON [code, détail], NULL si aucun échec
