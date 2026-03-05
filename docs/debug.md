# Guide de débogage pour LocalWriter

## 1. Fichier de log automatique

Le code génère automatiquement des logs dans `~/log.txt` (votre dossier utilisateur).

### Voir les logs en temps réel :

```bash
tail -f ~/log.txt
```

### Effacer les logs :

```bash
rm ~/log.txt
```

### Informations loguées :

- URL de l'endpoint
- Type d'API (chat/completions)
- Modèle utilisé
- Headers HTTP
- Données de la requête
- Statut de la réponse
- Erreurs éventuelles

## 2. Console LibreOffice (macOS)

### Lancer LibreOffice en mode console :

```bash
/Applications/LibreOffice.app/Contents/MacOS/soffice --writer
```

Les erreurs Python apparaîtront dans le terminal.

## 3. Débogage manuel avec des messages

Vous pouvez ajouter temporairement des affichages dans le document pour déboguer :

```python
text_range.setString(text_range.getString() + f"\nDEBUG: {variable_to_check}")
```

## 4. Tester l'API manuellement

### Test avec curl (OpenWebUI) :

```bash
curl -X POST http://localhost:3000/api/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama2",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

### Test avec curl (OpenAI) :

```bash
curl -X POST https://api.openai.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{
    "model": "gpt-3.5-turbo",
    "messages": [{"role": "user", "content": "Hello"}],
    "stream": true
  }'
```

## 5. Vérifier la configuration

Les paramètres sont stockés dans :
```
~/Library/Application Support/LibreOffice/4/user/localwriter.json
```

### Voir la configuration actuelle :

```bash
cat ~/Library/Application\ Support/LibreOffice/4/user/localwriter.json
```

### Exemple de configuration pour OpenWebUI :

```json
{
    "endpoint": "http://localhost:3000",
    "model": "llama2",
    "api_key": "",
    "api_type": "chat",
    "is_openwebui": true,
    "extend_selection_max_tokens": 70,
    "extend_selection_system_prompt": "",
    "edit_selection_max_new_tokens": 0,
    "edit_selection_system_prompt": ""
}
```

### Exemple de configuration pour OpenAI :

```json
{
    "endpoint": "https://api.openai.com",
    "model": "gpt-3.5-turbo",
    "api_key": "sk-...",
    "api_type": "chat",
    "is_openwebui": false,
    "extend_selection_max_tokens": 70
}
```

## 6. Erreurs courantes

### HTTP Error 405: Method Not Allowed
- Vérifiez que le bon type d'API est configuré (chat vs completions)
- Pour OpenWebUI, assurez-vous que `is_openwebui` est à `true`
- Testez l'URL avec curl pour confirmer le bon endpoint

### SSL: CERTIFICATE_VERIFY_FAILED
- Le code désactive maintenant la vérification SSL par défaut
- Si le problème persiste, vérifiez votre connexion réseau

### Pas de réponse / Timeout
- Vérifiez que le serveur est accessible : `curl http://localhost:3000`
- Vérifiez les logs : `tail -f ~/log.txt`
- Assurez-vous que le modèle existe sur votre serveur

### L'extension ne s'affiche pas dans le menu
- Redémarrez LibreOffice complètement
- Réinstallez l'extension : Outils → Gestionnaire des extensions

## 7. Réinstaller l'extension

```bash
cd /Users/etiquet/Documents/GitHub/localwriter
unopkg remove org.extension.sample
unopkg add localwriter.oxt
```

Puis redémarrez LibreOffice.

## 8. Mode développement

Pour modifier et tester rapidement :

```bash
cd /Users/etiquet/Documents/GitHub/localwriter

# Modifier le code
nano main.py

# Recréer le package
rm -f localwriter.oxt && \
zip -r localwriter.oxt Accelerators.xcu Addons.xcu description.xml main.py META-INF/ registration/ assets/

# Réinstaller
unopkg remove org.extension.sample
unopkg add localwriter.oxt

# Relancer LibreOffice
```
