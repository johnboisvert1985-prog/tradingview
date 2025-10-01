html += f'''
            <tr class="trade-row {row_class}">
              <td><strong>{symbol}</strong></td>
              <td><span class="badge badge-tf">{tf_label}</span></td>
              <td>{side_badge}</td>
              <td style="font-family:monospace;font-weight:700">{entry if entry else '—'}</td>
              <td>{tp_badge(r.get('tp1'), r.get('tp1_hit', False))}</td>
              <td>{tp_badge(r.get('tp2'), r.get('tp2_hit', False))}</td>
              <td>{tp_badge(r.get('tp3'), r.get('tp3_hit', False))}</td>
              <td>{sl_badge}</td>
              <td>{status}</td>
            </tr>
        '''
    
    if not rows:
        html += '<tr><td colspan="9" style="text-align:center;padding:60px;color:var(--muted)">✨ Aucun trade pour le moment</td></tr>'
    
    html += '''
          </tbody>
        </table>
      </div>
    </main>
  </div>
  
  <script>
  async function resetDatabase() {
    if (!confirm('⚠️ ATTENTION: Ceci effacera TOUTES les données de trading.\\n\\nUn backup sera créé automatiquement.\\n\\nContinuer?')) {
      return;
    }
    
    if (!confirm('Êtes-vous VRAIMENT sûr?\\n\\nCette action est irréversible.')) {
      return;
    }
    
    const secret = prompt('Entrez le webhook secret pour confirmer:');
    if (!secret) {
      return;
    }
    
    try {
      const res = await fetch('/api/reset-database', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({secret: secret})
      });
      
      const data = await res.json();
      
      if (res.ok && data.ok) {
        alert('✅ Base de données réinitialisée avec succès!\\n\\nBackup créé: ' + data.backup);
        window.location.reload();
      } else {
        alert('❌ Erreur: ' + (data.message || 'Secret invalide ou erreur serveur'));
      }
    } catch(e) {
      alert('❌ Erreur réseau: ' + e.message);
      console.error('Reset error:', e);
    }
  }
  </script>
</body>
</html>'''
    
    return HTMLResponse(content=html)

# =============================================================================
# SERVER STARTUP
# =============================================================================

if __name__ == "__main__":
    import uvicorn
    logger.info("Starting AI Trader Pro v2.1 Enhanced Server...")
    logger.info(f"Telegram: {'Enabled' if settings.TELEGRAM_ENABLED else 'Disabled'}")
    logger.info(f"Database: {settings.DB_PATH}")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
        log_level="info"
    )
