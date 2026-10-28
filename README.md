# KiCad MCP Server

Claude'un KiCad'de şemadan üretim dosyalarına kadar bütün akışı yürütmesini sağlayan **tek** MCP sunucusu (Claude Desktop'ta adı `KiCad`).

İki motoru birleştirir:
- **`kicad-cli`** (KiCad ile gelen resmi araç): ERC, DRC, BOM, kart istatistikleri, Gerber/delik zip'i, PDF, 3D render. Kod: `server.py`.
- **pcbnew motoru**: [mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server)'ın Python arka ucu (`python/kicad_interface.py`). Şema düzenleme, PCB yerleştirme, yol çekme, Freerouting ile autoroute. Node.js katmanı kullanılmaz; arka uç KiCad'in kendi Python'unda başlatılır ve satır başına bir JSON komutla konuşulur. Kod: `backend.py`.

KiCad 10.0.1 ile test edildi.

## Araçlar (30)

| Grup | Araçlar |
|---|---|
| Proje | `kicad_version`, `find_projects`, `new_project` |
| Kütüphane | `search_symbols`, `list_symbol_pins`, `search_footprints` |
| Şema | `add_schematic_parts`, `edit_schematic_part`, `delete_schematic_part`, `draw_schematic`, `list_components`, `run_erc` |
| PCB | `update_pcb_from_schematic`, `set_board_outline`, `add_mounting_hole`, `list_pcb_components`, `move_component`, `auto_place`, `route_pad_to_pad`, `autoroute`, `delete_tracks`, `add_copper_pour`, `set_design_rules`, `run_drc`, `board_stats`, `view_pcb`, `render_pcb_3d` |
| Üretim | `export_bom`, `export_manufacturing_files`, `export_schematic_pdf` |

Bizim eklediklerimiz (mixelpixx'te olmayan / farklı olan):
- `auto_place`: mixelpixx'in yönlendirme algoritmasından sonra parçaları kart çerçevesinin **içine, çakışmasız** satırlar halinde dizer (onların algoritması çerçeveyi dikkate almıyordu).
- `autoroute`: Freerouting'in sıkışık yerlerde daralttığı izleri kartın minimum iz genişliğine geri getirir.
- `export_manufacturing_files`: tüm üretim katmanlarını (iç bakırlar dahil) tek zip'te verir.
- `draw_schematic`: kabloları gerçekten çizilmiş, okunaklı küçük şemalar (eğitim devreleri).
- Her PCB aracı işi bitince kartı diske kaydeder; dosya dışarıdan değişirse otomatik yeniden yüklenir.

## Kurulum

Klasör yapısı:
```
mcp/
  kicad-mcp/            <- bu sunucu
  KiCAD-MCP-Server/     <- mixelpixx deposu (pcbnew motoru)
```

1. KiCad 9+ kurulu olmalı.
2. Bu sunucu ve bağımlılığı:
   ```
   git clone https://github.com/efedemirerr/kicad-mcp.git
   pip install -r kicad-mcp\requirements.txt
   ```
3. pcbnew motoru:
   ```
   git clone --branch stable https://github.com/mixelpixx/KiCAD-MCP-Server.git
   "<KiCad>\bin\python.exe" -m pip install -r KiCAD-MCP-Server\requirements.txt
   ```
   (Node.js / `npm install` gerekmez.)
4. Autoroute için Java 25 (Freerouting 2.5.0) ve `freerouting.jar`:
   - `~/.kicad-mcp/freerouting.jar` ← github.com/freerouting/freerouting sürümleri
   - Taşınabilir JRE: Adoptium Temurin 25 JRE zip'ini `~/.kicad-mcp/jre` içine açın (kurulum gerekmez).
5. Test: `python test_client.py "<proje klasörü>"`

Ortam değişkenleri (hepsi isteğe bağlı): `KICAD_CLI`, `KICAD_PYTHON`, `KICAD_MCP_BACKEND` (kicad_interface.py yolu), `JAVA_HOME`. Arka uç hataları `~/.kicad-mcp/logs/backend-stderr.log` dosyasına yazılır.

## Claude Desktop

**Settings → Developer → Edit Config** ile açılan dosyaya `claude_desktop_config.example.json` içeriğini ekleyin, `<KULLANICI>` geçen yolları kendi bilgisayarınıza göre düzeltin, Claude Desktop'ı tamamen kapatıp açın.

## Demo senaryosu

> "Desktop\mcp\demo klasöründe yeni bir KiCad projesi aç: 5V girişli iki LED'li (kırmızı, yeşil) bir gösterge kartı.
> Akım sınırlama dirençlerini hesapla, şemayı kur, ERC çalıştır, 45x30 mm karta yerleştir,
> autoroute yap, DRC temizse Gerber zip'ini ve 3D görüntüyü ver."

Bu akışın tamamı `led_board3` klasöründe test edildi: ERC 0, DRC 0 hata.

## Not: MCP SDK sürümü

Kod MCP Python SDK **2.x** ile yazıldı (`from mcp.server.mcpserver import MCPServer`).
1.x ile çalıştırmak için bu import'u `from mcp.server.fastmcp import FastMCP` ve `MCPServer(` → `FastMCP(` yapın.
