from pathlib import Path
import runpy, shutil, subprocess, tempfile
src=Path(__file__).resolve().parent
out=src/'dist'
out.mkdir(exist_ok=True)
name='runpod-gpu-desktop'
brand=runpy.run_path(str(src/'brand.py'))
version=brand['VERSION']
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    def deploy(source,relative,mode=0o644):
        path=root/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(src/source,path)
        path.chmod(mode)
    for file in ('brand.py','guide.py','gpu.py','pod_web.py','account.py','desktop.py','market.py','inventory.py','rental.py','jupyter_gpu.py','strata_bridge.py','strata_page.py','strata_pod.py','strata_source.py','strata_setup.py'):
        deploy(file,'usr/share/runpod-desktop/'+file)
    deploy('gpu-icon.png','usr/share/icons/hicolor/256x256/apps/jp.local.RunpodDesktop.png')
    deploy('jp.local.RunpodDesktop.desktop','usr/share/applications/jp.local.RunpodDesktop.desktop')
    deploy('jp.local.RunpodDesktop.service','usr/share/dbus-1/services/jp.local.RunpodDesktop.service')
    for unit in ('runpod-reservation.path','runpod-reservation.service'):
        deploy(unit,'usr/lib/systemd/user/'+unit)
    deploy('runpod-python','usr/bin/runpod-python',0o755)
    control=root/'DEBIAN/control'
    control.parent.mkdir()
    control.write_text(f"""Package: {name}
Version: {version}
Section: utils
Priority: optional
Architecture: all
Maintainer: {brand['AUTHOR']}
Depends: python3 (>= 3.10), python3-gi, python3-websocket, gir1.2-gtk-4.0 (>= 4.12), gir1.2-webkit-6.0, gir1.2-soup-3.0, gir1.2-secret-1, gnome-keyring, openssh-client, rsync, libglib2.0-bin, systemd, ptyxis, curl
Description: {brand['PUBLIC_NAME']}
 Reserve a GPU, manage its power and open a connected JupyterLab.
 Account sign-in, SSH setup and reservation notifications are integrated.
""")
    dest=out/f"{name}_{version}_all.deb"
    root.chmod(0o755)
    for folder in root.rglob('*'):
        if folder.is_dir():
            folder.chmod(0o755)
    control.chmod(0o644)
    subprocess.run(['dpkg-deb','--root-owner-group','--build',str(root),str(dest)],check=True)
