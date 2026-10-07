#!/usr/bin/env python3
# =============================================================================
# idGuru — AI-Powered Underwater Species Identifier
# Copyright (c) 2025-2026 Kaj Maney / liquidGuru (liquidguru.com)
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public License
# for more details. You should have received a copy of the licence along with
# this program. If not, see <https://www.gnu.org/licenses/>.
#
# The author's underwater footage, the screenshots under docs/, and any
# dataset derived from them are NOT covered by the AGPL and remain licensed
# under CC BY-NC 4.0. See LICENSE-MEDIA.
#
# Contact: kaj@liquidguru.com
# =============================================================================
"""
idGuru entry point for PyInstaller build.
Runs Flask server in background and shows a system tray icon.
"""
import sys
import os
import threading
import webbrowser
import time

# Fix the ffmpeg/ffprobe path to look in the bundle directory first
if getattr(sys, 'frozen', False):
    bundle_dir = sys._MEIPASS
    os.environ['PATH'] = bundle_dir + os.pathsep + os.environ.get('PATH', '')

URL = 'http://localhost:5001'

def run_as_exe():
    """Run as a bundled exe with system tray icon."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        # pystray not available — fall back to headless + open browser
        sys.argv.append('viewer_headless')
        def open_browser():
            time.sleep(2)
            webbrowser.open(URL)
        threading.Thread(target=open_browser, daemon=True).start()
        from underwater_indexer import main
        main()
        return

    # Start Flask server in background thread
    sys.argv.append('viewer_headless')
    server_thread = threading.Thread(target=_run_server, daemon=True)
    server_thread.start()

    # Wait for server to start then open browser
    def open_browser():
        time.sleep(2)
        webbrowser.open(URL)
    threading.Thread(target=open_browser, daemon=True).start()

    # Create tray icon
    icon_img = _make_tray_icon()

    def on_open(icon, item):
        webbrowser.open(URL)

    def on_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem('Open idGuru', on_open, default=True),
        pystray.MenuItem('Quit', on_quit)
    )

    tray = pystray.Icon('idGuru', icon_img, 'idGuru — Underwater ID', menu)
    tray.run()


def _run_server():
    """Run the Flask server."""
    from underwater_indexer import main
    main()


def _make_tray_icon():
    """Load the idGuru favicon as the tray icon."""
    import base64, io
    from PIL import Image
    b64 = "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAASwUlEQVR4nO2beZBdVZ3HP+ec+7Z+vSfppLOQjTQhnZAIgoKRpXQcwbUGJCPKMmGbERQjSOmU42ihlFKODqOOI6Vx0LIGIYYlIYQAWYBMoAySBJhAEgIh6dDdr9Pr29+958wf59777nvdDDXzT1cNOVXJe+/mLL/f9/c7v9/3/M6NGB7NGd7DTU62AJPdTgIw2QJMdjsJwGQLMNntJACTLcBkt5MATLYAk90cY97TRBAnEY/VPPj/DIeY6JmJuMBE3mCMqQ4UE03xf2lmAnGCtUXd9//NnNExZtwjYwxSqppRIQBaa6R8L4QEY40qrK7CGGM8r4JSMXbv3s3W7dvpe/ttKhUXE0URag0ykRHHrxX9iNjGPhG+R0U9L3gWjhV1y9SsW+8tZoJPkFJitGbmrJncfNNNpNNpjLHPheu6RinFE09s4altO7jxhhsol8u4nlejgBEGacBEBAuWEf4XY0BU1fPlfHekDMYqakRdf7v9ojvT9ov2MgSbNBglQtn8+QwoR/LYY5vZufNZ7r33XpKJBEpJHNDk8wWefGobt91+O9Pa23C1CcUHa6FwYeNjK4LF7RoyFEH4AGiMMFapoFONxUU4vtZm/rOo0uF6USgFBoEwOpStHj4LRvXX19YsplAqsn79eq668kpct4IjhKA/04+QgtbmZrL5Ihi7T4I/E1otQALQQvjWixrPqiKM8S04HtTweyC5sSCZyByYQHnpK2N8xQm9zh+MMBpqVrEACCHQxtDa3Eh3dzd7/rybIBY4IMn0DXD8+HGkFLXK1wkbWskYjBAEdjc6sK6oCuRbWQd/m6rfBpnFKmbHiHBvmRoX1xhMuI4GIxBShHgZY0FGhBsGaSJ+IHxIjEEpxeCJQQ6//jqedgGDI6Wkt6+Xvt5elJRora0VALSuA8Iqp4UBFGgQSpJIpVCOxUCEgkNhrIjnVtAiUKw2qFathw9UbdrVvrWD7aacBIl0EulU1xGAW4ZcdtTGHyPwtEYGW06DFjo0ouM47N27j1KxRCwWxwGoVCrV4IZvjUD5AICIFwQ6SCWRTpye40fBeDhKUqmUGR7JohGcurCLmBOnUiliABlYHnxQqjtGRxQ3kU8jBGiDiidwMfQeOYyUAimgUCwxls3SmGpmwYKFlPI5wKvOYYJwXPViz/XwPA+trZc4oZ9EzGL8AaZuorALAuNpmppT7Hj2WT778Y9hjKFYLNLRMY1rr1uN0YY5p57G9deuZiCTR0pqvCkyGTq6xSKBUvtPPU8zpSXFP37nO/zozjuRQlIul1l8ehc33HADr79+iM+suopzP3AOI4NDKGHCuerXTDemcZRjvUcYC4BSClVHggIQxivvWwyBI2F0dIhCoYDjOPzsZz/hwgvPp7v7TI4dfYPf/WGdXUAoQKOFjd0ycgbT9fP7W1CjffAl2g9smUw/lUqFrq5F/OSf72LpsqWcMvtU1q//A319PSRiEoOw6xgPjB84RRgt8VzP5hk/hkkAFXOQKqCI73wasB4RRFprKcdxbJTVmlmz59DdfSYjI0N869v/QD6bs72ErPqYH7iqGUaHFg+spUMuYWOS5wfTWCzmKwQXX3wJp8w+FU+77Nixg2QiYeeXYpwKUW9OplIo5WCMB2gLgIxuAUQ1RUVTVTiR8AO6L6wJqKXgc5f9NfPmzWP5ihXc+++/J5lK+mNNlVUaa2Xjp0ntQ2qEJVthCkRijEBrg6ttH8+z4w4cOMimRzdhDOz6z6f56U9/QTrd4EMprHFNVKeqLlp7GKP97YH1UM9zbfQPMRA2f9chGCIhNEYLPKDs6nCM67ocOXIEsNuqoq0bG60xwudlwmCE9HOzRqDseuH04ckFg7Yco85EGBgY6EcI2LfvJatILB4SKI1N0CEzjAyNxxMoqWzqhiAI1s5ubFKticb1fTCGsgeOY63suW5ND8/ziMWSuD4ASDBG2u0QWMgIPHwFg+Dnk2kduK0wSKFqFAK79YBaw/nqjiNcdadYU+1qAYjF4ihHhQIEfFcYa6Xa6X1vkoKR4RxnfvACnjv8JnE8YkKBANdoyq5Hx5TZDI6WUUratBowRyOQsThNTQ2k1IRWCFsZGHJBeF4NANq3YLFUCuWy8SbYnFXlDVRB8DsGVMkBKJWKuK4X4hMoOUHSqkFQKUUpn+O57VvRlTK64uG5LsaRxBNJzv/wR5k3ZzalsrHBCYEWiqlTGjHAwYOH2PPiCxzYv5+eYz0UCgVyuRzJVBKlHKZ1TGPp8uWcf8EFTJ87DyWlPdlFKHrM94TqIdK3f30gDB9VN4ZPhatBqWaAMJgJ9DfhYGhrTfLMs1v56urVAMRjDu3tbXz/zjtIJWJsfmQdt339NvI5gaMUCEVHW4rHNm/mJ3f9kO3btgMWm7lzT2HGjOm0tLaQTipiGHreOMQre/7M/ffey6WXr8KtVEKXb2tvAyyJizYBPssCI6vRIzgsRSm5ED4RisUTVkB/Ci0MRteetu1Yg/ahFkKiAKkEynGYOqWdJ5/cxPz5C0in2zjw2n6OPLrJ9pFxjJRMb03wox//mG/ceisAV3xhFVdf9UWWLVtKZ+dcand5tblegQfuX8e2p57kwgvPx61UWHzaIkBH0ndA2uxflnlWp6zuexN+GOMD4LoVPM8LlfRPGZak1LmBCA9C1tUUNgD29fXz1tFjLF16Flpr1q79FSKRxgE8DdOnJFj3yCOh8mu+djM//qefAnC89ygPrLuPN994i/5MhpK/r9MNDXR2dtLdvYTLLrucFStWMG1qK6NjI+E5IhHk/xo26YdSX3QZHFIiEBsMQkTTYBDsfE4eHE6iLCoIHkCY1qI+8uD6h7j4459keGSAn//8F3zlttsBiDU4jObzfGPNGoQQdHcv4fvfu5MjRw7x9du/weObtzA6OjaB7auto6ODT336E3zwA+/nIx+5EM9zAW+cz4h6t63ToZ7XWiaofDYXgah6RK8SHj+c+jy6FlGwniSEZmRkhGKxhOM4eEBrOsaWzZt58/BhAC6//FJSqSa++907eOD+P76r8gD9/f38+le/4frrb2Lt2t/S1NSEwYSeq322aPxoJ0zE1nXKR7OinwYd0un0hNHTjvRZVECIhAhjSX0WBoHrlnE9XVOheXzjxvBw0t29mJ6e13nooQ0ox0G7Ln956WWc/7FPMH3mQpSK++UETTF7gj27n+U/7vkl+WwWrTX33beOa665EoETCYJVQ0l8ym4EQhgsqwy6iRoVJYDWMK1j+nh3CqeuLS0FiAomClsGgfSLKxbhgta8vHefTTuOw/z58zl48BBDQ0N4rsvX7vg+d697gIs+eQ3DwwV2bnuKrZs2cOjVA0ybtYQvf/cH3L3uUZyYg9aa/v4Mg4MnABVmhbA+EGGVVfldELbQEtUDIywAruuyqKsrooIJg2F9xVYExboJKUIUpGqXkZEReo/3ANDW3kpnZyelUgUhBO1T2vnctV/m7YM5ho8epPe15xk9/iru6DHeemknD639Ea/seo2zzzuPs849D4BcLsfbb/eF69SsboTPJoVN5RAWW40IgnwwwOcBQgiam5vq+L6dMlpMCFB7t1Zf7s5mx8hmswCkkiniiRhOLIYxhtnzF9CYbkTmhmhpS/G3X7qJmGPJjjYwms1zYiSPV9DMWtAFW7fieR4nBgcBKBYLVCWFoJ5WJUR+0VSA0YZUKkUsFvOBCwsi2j4MYbRFTkOtBxgMVYcLYaxXH8/omkCTy+XD1JZIJHCUQ3NzEwDpxkaa0gISCseTlPMjFHUQPwRKKJob4sQdieNUr/EKeat4PB4PZfPRD6J3RLSgMFI9egfyOwBSKpLJFEF21/6JvEZ5/8hbW9Csp8v2u+e6lqv7fVzXxfP3aqlYJJ8vsGDBAuIxh3K5REpBQUmkBqQiJqzyWtgCRwxJMoGt80WABmpA8QW10BkNxsMIFWYvz3NJJBNMnTo1PLzJQG4pq5QpTIER5euVrP1WIwGOqj3eNDamw4JFuVIhX8gzpb2Dr371y2SHR3CwxQ6kgxHSKu5fXSEkUsVJxAwR0kdTUyMAhULBX7V6jPbLzGFANMYghaRULDF33jyWdC+hUCwiZaQ2FXhOsAWqz6vARMJhTYaobbYuEIw1QLKxiYZ0GoATJwbJZAYAwS1rbqb79C6yY2PEGxyMSCBUHCPjGOmA/x0nTqMUDGds4EskEsyYMQPwWR5Qc5MSlbvmu0EpiVSyrihaX22IVmjD78Z3a4ExtuRQzwGCvaW1f7KUAhdobGlh6rRpZDIZKpUKfb39GAMzOxdwycUfZ/umTVy5ahUDqRYqJdCuBgxCKoSCKUl4K9PPczueBqClpZlZs2YAXk1NUdR8EdS//2GMrRbFY3E/ZfpMUAgZQckFtI9D7VkgWpSoZ1cR7PH8s7oQFoB4LMaCxaeFPZ5++hmEEFQqFT7/+S/y4vO7+OFdd7H/hT+R6TvCaHaA0ewAmf4jHN6/h1/d80suev/Z9PVZD1i9+ira2lotAH5skUaEBgnvBMc1gxSCRDIRxi5bExQicp62PEoYPeHZLFIsH7eElArPg5BrGcsUtYH3nfshv4/knnvW8sp/vUgsFiORSPGDH/wQzyty3Reu4JzTFrFi3hyWz53DigXzWHnm+7j1llvomNrOypXnEY/HOH3J6RgjcF0dAhDcIldrBRNHKIT03xGwIV8CeFrXlZYEZlwoDBHA3pt4uIAbGTY2NoZSDtIPgkLauD02ZPjwRz+F4yikVIyNjXHFFVfzwgvPo5QkHk/w99/8Nnv27WbbjifZsPFBNm58kG3bt/Di3j8xMjrI/Q/ch1IK1/W4/rov8cwzO3GcJOVyOSraOHNBXRAXhFeAEKlG2d+aKruPWtqv0Rl/ayDRxh5zPb+7VIpNmx7n0ks/y+DgkP/Q+kJuZJT5C7v45Kqreej3a4knEuzb+xLnnruSSy65mLPefxZdixbRObOTKVPaaWtrByCfL3Dg1UP87F9+wfoHH2RocAgp7aXIjTfezKZNT7Br1y4AXLdagPU3dih/NJALExyPrQc4xhji8Tglr/pCRFCs1jrKBYJjso2eRhvcMlTKAqkUSjkUiiXWr38YsOUy17NwOtJjpP8Ea75zN4OZfp7eshGASsXl4Yc38PDDG8bZbqK2eMnpHDpwCCklI6Nj/Pa3vwvXKlc0rusfggI3rdvEwf1FLl8g2KiOEIKK61Iul9HaFhu1Nmiv7hwQlJYCeIzGrbjkc3m056E9r2Yxz/PIjhZsTPCKJKXk0GsH+NQX/o6l56xk6yN/4OAre/G88efJ+tbc3MzqW7/OqYu7uXnVX437d8/zKJVcvIq2Fx7jXisJlcBgyOdyQcAILkdLjI2NhjUzMeHoKopo60a50SEWLFzIldd/hab2DppaWzFAfnSM/NgwKy/4KKMnxnCERjmKBuXyrRsvY9k5K7n8ujU48QRjI0MM9vYwPDRAMZcjNzaKwdDU1EJz+1TmLFrC8rM/RFf3Yt56/S2uXfNtGpqaSCWTeJ7HcKaPuBKcMnc+2dFhlBDWthOUw6WUFAtFyuVyyB+cwLoC6fPb6t4J6G+0+RsAjEZJyA5l2Lzhj8ycM59F3SsQSnF4/0v09hzhs5/5NKZcQCmBWy5yRvcS7v63X3Pbzdez++knSKabWH7OSrpOX07XsrNpbG4llWzE81yGMm/T13uU57Y+zvrf/Cu3fPMOdjyxked37mDmKfOZM3choyODHHj5RZYtO4PW5mYqxQLxuALGy621JpFI8sabb7J3715SqSRaa98DyhW2b9vO31xzdah4GFNN9TWW4Lc29nqplC8zY+oUlp+xjFde3sdw5hiVUgmE4C8u/gRT2xrx3KK9hTGaTF8PZyxdwgMbtvD4Y4+ybctmnt/2GM9ve+wdPQ7gK7d9k3PPPgOdH+DVPbs4/PKfeHHnEzSmG4knkpwyuxO8kjV4EASDIzD4xE3gOA59vb0cO3qUmOPYK7JcvmiOHz/O/ldf5cKLLsKrVPxB75BHTfXiYSybRSmHlpY2iqUSsXiCSrnMiYEMs2bPplwqWlboFyIGBwc5MZBheudMOqbPwPM0L+3bQ39fH1JKtPYQQuK6FfsGlxA0N7cwc/ZsWppbaEinMQiO9xyjsbGRhoY0JwYytLe3o5S0yTu4eYq04Gc8kaCnp4dDBw+ycuWHkFIhhkayJhaLkUzGyY7lqzTyHeJItAWXFPYdQ5sZhBQoqahUKoi6K3elFI5jy1iu6yKEoKGhAUe9wwLYNFsuldERrhLUErTWKCUjlzrvYLPgU2vi8TiJRIxsNm89e3g0Z4yf4+vfony3VvWSCUux4ywRsLTgxSXLtMdfytQ3+wJnSPVqYlOwPev3/P8kswXO6ipO/re593g7CcBkCzDZ7SQAky3AZLeTAEy2AJPd3vMA/DdJ6PnzxWqm2wAAAABJRU5ErkJggg=="
    data = base64.b64decode(b64)
    img = Image.open(io.BytesIO(data)).convert('RGBA')
    return img.resize((64, 64), Image.LANCZOS)


if getattr(sys, 'frozen', False) and len(sys.argv) == 1:
    run_as_exe()
else:
    from underwater_indexer import main
    main()
