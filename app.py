import os
import json
import uuid
import qrcode
import base64
import zipfile
from datetime import datetime
from io import BytesIO
from PIL import Image

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, Response

app = Flask(__name__, static_folder='static', template_folder='templates')
app.secret_key = 'photo-booth-event-secret-key-2024'

# ==================== CONFIGURATION ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024  # 32MB max
app.config['DATA_FILE'] = os.path.join(BASE_DIR, 'photobooth_data.json')
app.config['LOCK_FILE'] = os.path.join(BASE_DIR, 'camera_lock.json')
app.config['TEMPLATES_FILE'] = os.path.join(BASE_DIR, 'frame_templates.json')

# เก็บรูปภาพและกรอบใน RAM
PHOTOS_IN_MEMORY = {}
FRAME_IMAGES = {}

# สร้างโฟลเดอร์ที่จำเป็น
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# ==================== FILE OPERATIONS ====================
def load_data():
    try:
        with open(app.config['DATA_FILE'], 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            'latest_qr': None,
            'photos': [],
            'stats': {
                'total_photos': 0,
                'total_downloads': 0,
                'total_sessions': 0,
                'retake_used': 0
            }
        }

def save_data(data):
    with open(app.config['DATA_FILE'], 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_lock_status():
    try:
        with open(app.config['LOCK_FILE'], 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {
            'camera_locked': False,
            'locked_by_code': None,
            'locked_at': None,
            'retake_available': True,
            'camera_enabled': True
        }

def save_lock_status(status):
    with open(app.config['LOCK_FILE'], 'w', encoding='utf-8') as f:
        json.dump(status, f, ensure_ascii=False, indent=2)

def load_frame_templates():
    try:
        with open(app.config['TEMPLATES_FILE'], 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        default = {'templates': []}
        save_frame_templates(default)
        return default

def save_frame_templates(templates):
    with open(app.config['TEMPLATES_FILE'], 'w', encoding='utf-8') as f:
        json.dump(templates, f, ensure_ascii=False, indent=2)

# ==================== UTILITIES ====================
def generate_qr_code(url):
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffered = BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return f"data:image/png;base64,{img_str}"

def create_composite_image(photos_base64, template_id):
    """รวม 4 รูปเป็นรูปเดียวตามกรอบ"""
    try:
        templates = load_frame_templates()
        template = next((t for t in templates['templates'] if t['id'] == template_id), None)
        if not template:
            return None
        
        canvas_width = template['canvas_width']
        canvas_height = template['canvas_height']
        
        # สร้าง canvas
        if template.get('frame_image_id') and template['frame_image_id'] in FRAME_IMAGES:
            frame_bytes = FRAME_IMAGES[template['frame_image_id']]
            canvas = Image.open(BytesIO(frame_bytes)).convert('RGBA')
            canvas = canvas.resize((canvas_width, canvas_height), Image.Resampling.LANCZOS)
        else:
            canvas = Image.new('RGBA', (canvas_width, canvas_height), (255, 255, 255, 255))
        
        # วางรูปทั้ง 4 รูป
        positions = template['photo_positions']
        for i, photo_base64 in enumerate(photos_base64[:4]):
            if i >= len(positions):
                break
            
            pos = positions[i]
            if ',' in photo_base64:
                photo_base64 = photo_base64.split(',', 1)[1]
            
            photo_bytes = base64.b64decode(photo_base64)
            photo_img = Image.open(BytesIO(photo_bytes)).convert('RGBA')
            
            # ปรับขนาดและครอป
            target_width, target_height = pos['width'], pos['height']
            img_aspect = photo_img.width / photo_img.height
            target_aspect = target_width / target_height
            
            if img_aspect > target_aspect:
                new_height = photo_img.height
                new_width = int(new_height * target_aspect)
                left = (photo_img.width - new_width) // 2
                photo_img = photo_img.crop((left, 0, left + new_width, new_height))
            else:
                new_width = photo_img.width
                new_height = int(new_width / target_aspect)
                top = (photo_img.height - new_height) // 2
                photo_img = photo_img.crop((0, top, new_width, top + new_height))
            
            photo_img = photo_img.resize((target_width, target_height), Image.Resampling.LANCZOS)
            canvas.paste(photo_img, (pos['x'], pos['y']))
        
        output = BytesIO()
        canvas.convert('RGB').save(output, format='JPEG', quality=95)
        output.seek(0)
        return output.getvalue()
    except Exception as e:
        print(f"❌ Composite error: {e}")
        import traceback
        traceback.print_exc()
        return None

# ==================== ROUTES ====================
@app.route('/')
def index():
    return redirect(url_for('capture'))

@app.route('/capture')
def capture():
    lock_status = load_lock_status()
    templates = load_frame_templates()
    return render_template('capture.html', 
                          camera_locked=lock_status['camera_locked'],
                          retake_available=lock_status['retake_available'],
                          camera_enabled=lock_status['camera_enabled'],
                          frame_templates=templates['templates'])

@app.route('/qr')
def qr_display():
    return render_template('qr_display.html')

@app.route('/admin')
def admin():
    data = load_data()
    lock_status = load_lock_status()
    templates = load_frame_templates()
    return render_template('admin.html', 
                          photos=list(reversed(data['photos'])), 
                          stats=data['stats'],
                          latest_qr=data.get('latest_qr'),
                          camera_locked=lock_status['camera_locked'],
                          locked_by_code=lock_status['locked_by_code'],
                          frame_templates=templates['templates'])

@app.route('/scan/<code>')
def scan_code(code):
    data = load_data()
    for photo in data['photos']:
        if photo['pickup_code'] == code:
            data['stats']['total_downloads'] += 1
            save_data(data)
            
            lock_status = load_lock_status()
            if lock_status['locked_by_code'] == code:
                lock_status.update({
                    'camera_locked': False,
                    'locked_by_code': None,
                    'retake_available': True
                })
                save_lock_status(lock_status)
                data['latest_qr'] = None
                save_data(data)
            
            return redirect(url_for('download', code=code))
    
    return render_template('download.html', error="ไม่พบรหัสดังกล่าว")

@app.route('/download/<code>')
def download(code):
    data = load_data()
    for photo in data['photos']:
        if photo['pickup_code'] == code:
            base_url = request.host_url.rstrip('/')
            return render_template('download.html', 
                                 photo_info=photo,
                                 base_url=base_url)
    return render_template('download.html', error="ไม่พบรหัสดังกล่าว")

@app.route('/favicon.ico')
def favicon():
    transparent_icon = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
    )
    return Response(transparent_icon, mimetype='image/png')

# ==================== API - UPLOAD ====================
@app.route('/api/upload', methods=['POST'])
def upload_photo():
    try:
        data = request.get_json()
        if not data or 'photos' not in data:
            return jsonify({'error': 'ไม่มีข้อมูลรูปภาพ'}), 400
        
        photos_data = data['photos']
        template_id = data.get('template_id')
        pickup_code = str(uuid.uuid4())[:8].upper()
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        saved_files = []
        
        # ถ้าเลือกกรอบ
        if template_id and template_id != 'none':
            composite_bytes = create_composite_image(photos_data, template_id)
            if composite_bytes:
                filename = f"{timestamp}_{pickup_code}_composite.jpg"
                PHOTOS_IN_MEMORY[filename] = composite_bytes
                saved_files.append(filename)
                print(f"✅ Composite saved to RAM: {filename}")
            else:
                template_id = None
        
        # ถ้าไม่เลือกกรอบ
        if not template_id or template_id == 'none':
            for i, photo_data in enumerate(photos_data):
                if ',' in photo_data:
                    photo_bytes = base64.b64decode(photo_data.split(',', 1)[1])
                    filename = f"{timestamp}_{pickup_code}_{i+1}.png"
                    PHOTOS_IN_MEMORY[filename] = photo_bytes
                    saved_files.append(filename)
                    print(f"✅ Photo saved to RAM: {filename}")
        
        if not saved_files:
            return jsonify({'error': 'ไม่สามารถบันทึกรูปได้'}), 500
        
        # สร้าง QR Code
        base_url = request.host_url.rstrip('/')
        qr_url = f"{base_url}/scan/{pickup_code}"
        qr_code = generate_qr_code(qr_url)
        
        # บันทึกข้อมูล
        data_store = load_data()
        photo_record = {
            'id': str(uuid.uuid4()),
            'pickup_code': pickup_code,
            'filenames': saved_files,
            'timestamp': datetime.now().isoformat(),
            'time_display': datetime.now().strftime('%d/%m/%Y %H:%M'),
            'download_count': 0,
            'qr_url': qr_url,
            'retake_used': False,
            'template_id': template_id if template_id and template_id != 'none' else None,
            'is_composite': len(saved_files) == 1 and '_composite' in saved_files[0]
        }
        
        data_store['photos'].append(photo_record)
        data_store['stats']['total_sessions'] += 1
        data_store['stats']['total_photos'] += len(saved_files)
        
        # อัปเดต latest_qr
        data_store['latest_qr'] = {
            'code': pickup_code,
            'qr_image': qr_code,
            'timestamp': photo_record['timestamp'],
            'url': qr_url,
            'time_display': photo_record['time_display']
        }
        
        save_data(data_store)
        
        # ล็อคเครื่องถ่ายรูป
        lock_status = load_lock_status()
        lock_status.update({
            'camera_locked': True,
            'locked_by_code': pickup_code,
            'locked_at': datetime.now().isoformat()
        })
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'pickup_code': pickup_code,
            'qr_code': qr_code,
            'qr_url': qr_url,
            'filenames': saved_files,
            'is_composite': photo_record['is_composite'],
            'storage': 'RAM'
        })
    except Exception as e:
        print(f"❌ Upload error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ==================== API - PHOTO RETRIEVAL ====================
@app.route('/photo/<filename>')
def get_photo(filename):
    if filename in PHOTOS_IN_MEMORY:
        mimetype = 'image/jpeg' if filename.endswith('.jpg') else 'image/png'
        return Response(PHOTOS_IN_MEMORY[filename], mimetype=mimetype)
    return "Not found", 404

@app.route('/api/download_all/<code>')
def download_all(code):
    try:
        data = load_data()
        photo_info = None
        
        for photo in data['photos']:
            if photo['pickup_code'] == code:
                photo_info = photo
                break
        
        if not photo_info:
            return jsonify({'error': 'ไม่พบรหัสดังกล่าว'}), 404
        
        data['stats']['total_downloads'] += 1
        save_data(data)
        
        # ถ้าเป็น composite ส่งไฟล์เดียว
        if photo_info.get('is_composite') and len(photo_info['filenames']) == 1:
            filename = photo_info['filenames'][0]
            if filename in PHOTOS_IN_MEMORY:
                return send_file(
                    BytesIO(PHOTOS_IN_MEMORY[filename]),
                    mimetype='image/jpeg',
                    as_attachment=True,
                    download_name=f'photobooth_{code}.jpg'
                )
        
        # สร้าง ZIP
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, filename in enumerate(photo_info['filenames']):
                if filename in PHOTOS_IN_MEMORY:
                    ext = 'jpg' if filename.endswith('.jpg') else 'png'
                    zip_file.writestr(f'photo_{i+1}.{ext}', PHOTOS_IN_MEMORY[filename])
        
        zip_buffer.seek(0)
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'photos_{code}.zip'
        )
    except Exception as e:
        print(f"❌ Download all error: {e}")
        return jsonify({'error': str(e)}), 500

# ==================== API - FRAME TEMPLATES ====================
@app.route('/api/frame_templates')
def get_frame_templates():
    templates = load_frame_templates()
    # เพิ่ม URL สำหรับรูปกรอบ
    result_templates = []
    for template in templates.get('templates', []):
        template_copy = template.copy()
        if template.get('frame_image_id'):
            template_copy['frame_image'] = f"/frame/{template['frame_image_id']}"
        else:
            template_copy['frame_image'] = None
        result_templates.append(template_copy)
    return jsonify({'templates': result_templates})

@app.route('/api/frame_templates', methods=['POST'])
def create_frame_template():
    try:
        data = request.get_json()
        template_id = str(uuid.uuid4())[:8]
        template = {
            'id': template_id,
            'name': data['name'],
            'orientation': data['orientation'],
            'canvas_width': int(data['canvas_width']),
            'canvas_height': int(data['canvas_height']),
            'photo_positions': data['photo_positions'],
            'frame_image_id': None,
            'created_at': datetime.now().isoformat()
        }
        
        if 'frame_image' in data and data['frame_image']:
            frame_image_base64 = data['frame_image'].split(',', 1)[1] if ',' in data['frame_image'] else data['frame_image']
            frame_bytes = base64.b64decode(frame_image_base64)
            frame_id = f"frame_{template_id}"
            FRAME_IMAGES[frame_id] = frame_bytes
            template['frame_image_id'] = frame_id
            print(f"✅ Frame image saved to RAM: {frame_id}")
        
        templates = load_frame_templates()
        templates['templates'].append(template)
        save_frame_templates(templates)
        
        return jsonify({'success': True, 'template': template})
    except Exception as e:
        print(f"❌ Create template error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/frame_templates/<template_id>', methods=['DELETE'])
def delete_frame_template(template_id):
    try:
        templates = load_frame_templates()
        templates['templates'] = [t for t in templates['templates'] if t['id'] != template_id]
        frame_id = f"frame_{template_id}"
        if frame_id in FRAME_IMAGES:
            del FRAME_IMAGES[frame_id]
        save_frame_templates(templates)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/frame_templates/<template_id>/activate', methods=['POST'])
def activate_frame_template(template_id):
    """API endpoint สำหรับเปิดใช้งานกรอบ"""
    # ปัจจุบันยังไม่มีการเก็บสถานะ active แยกต่างหาก
    # สามารถเพิ่ม logic การเก็บสถานะได้ในอนาคต
    return jsonify({'success': True, 'message': f'Template {template_id} activated'})

@app.route('/frame/<frame_id>')
def get_frame_image(frame_id):
    if frame_id in FRAME_IMAGES:
        return Response(FRAME_IMAGES[frame_id], mimetype='image/png')
    return "Not found", 404

# ==================== API - STATUS & CONTROL ====================
@app.route('/api/stats')
def get_stats():
    """API สำหรับดึงสถิติระบบ"""
    data = load_data()
    total_bytes = sum(len(photo_bytes) for photo_bytes in PHOTOS_IN_MEMORY.values())
    total_mb = total_bytes / (1024 * 1024)
    
    return jsonify({
        'total_sessions': data['stats']['total_sessions'],
        'total_photos': data['stats']['total_photos'],
        'total_downloads': data['stats']['total_downloads'],
        'total_retakes': data['stats']['retake_used'],
        'memory_mb': round(total_mb, 2)
    })

@app.route('/api/recent_photos')
def get_recent_photos():
    """API สำหรับดึงรูปล่าสุด"""
    data = load_data()
    return jsonify(list(reversed(data['photos'])))

@app.route('/api/latest_qr')
def latest_qr():
    data = load_data()
    return jsonify(data.get('latest_qr', {}))

@app.route('/api/camera_status')
def camera_status():
    return jsonify(load_lock_status())

@app.route('/api/full_status')
def full_status():
    data_store = load_data()
    lock_status = load_lock_status()
    return jsonify({
        'camera_locked': lock_status['camera_locked'],
        'locked_by_code': lock_status['locked_by_code'],
        'retake_available': lock_status['retake_available'],
        'camera_enabled': lock_status['camera_enabled'],
        'latest_qr_exists': data_store.get('latest_qr') is not None,
        'latest_qr_code': data_store.get('latest_qr', {}).get('code'),
        'total_sessions': data_store['stats']['total_sessions'],
        'total_photos': data_store['stats']['total_photos'],
        'server_time': datetime.now().isoformat()
    })

@app.route('/api/retake', methods=['POST'])
def retake_photo():
    try:
        lock_status = load_lock_status()
        
        if not lock_status['retake_available']:
            return jsonify({'error': 'ถ่ายใหม่ได้แค่ 1 ครั้งเท่านั้น'}), 403
        
        if not lock_status['camera_locked']:
            return jsonify({'error': 'ไม่มีเซสชันที่ต้องถ่ายใหม่'}), 400
        
        data_store = load_data()
        if lock_status['locked_by_code']:
            for photo in data_store['photos']:
                if photo['pickup_code'] == lock_status['locked_by_code']:
                    photo['retake_used'] = True
                    data_store['stats']['retake_used'] += 1
                    break
        
        save_data(data_store)
        
        lock_status.update({
            'camera_locked': False,
            'retake_available': False
        })
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'message': 'พร้อมถ่ายรูปใหม่ (ครั้งสุดท้าย)'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/unlock_camera', methods=['POST'])
def unlock_camera():
    try:
        lock_status = load_lock_status()
        lock_status.update({
            'camera_locked': False,
            'locked_by_code': None,
            'retake_available': True
        })
        save_lock_status(lock_status)
        
        data_store = load_data()
        data_store['latest_qr'] = None
        save_data(data_store)
        
        return jsonify({
            'success': True,
            'message': 'ปลดล็อคกล้องสำเร็จ'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/toggle_camera', methods=['POST'])
def toggle_camera():
    try:
        data = request.get_json()
        enabled = data.get('enabled', True)
        
        lock_status = load_lock_status()
        lock_status['camera_enabled'] = enabled
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'camera_enabled': enabled,
            'message': f"{'เปิด' if enabled else 'ปิด'}กล้องสำเร็จ"
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/emergency_unlock', methods=['POST'])
def emergency_unlock():
    try:
        lock_status = load_lock_status()
        lock_status.update({
            'camera_locked': False,
            'locked_by_code': None,
            'retake_available': True
        })
        save_lock_status(lock_status)
        
        data_store = load_data()
        data_store['latest_qr'] = None
        save_data(data_store)
        
        return jsonify({
            'success': True,
            'message': 'ปลดล็อคฉุกเฉินสำเร็จ'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== API - ADMIN ====================
@app.route('/api/delete_photo/<photo_id>', methods=['DELETE'])
def delete_photo(photo_id):
    try:
        data_store = load_data()
        
        photo_to_delete = None
        for i, photo in enumerate(data_store['photos']):
            if photo['id'] == photo_id:
                photo_to_delete = photo
                data_store['photos'].pop(i)
                break
        
        if not photo_to_delete:
            return jsonify({'error': 'ไม่พบรูป'}), 404
        
        deleted_count = 0
        for filename in photo_to_delete['filenames']:
            if filename in PHOTOS_IN_MEMORY:
                del PHOTOS_IN_MEMORY[filename]
                deleted_count += 1
        
        data_store['stats']['total_photos'] -= len(photo_to_delete['filenames'])
        data_store['stats']['total_sessions'] -= 1
        data_store['stats']['total_downloads'] -= photo_to_delete.get('download_count', 0)
        
        save_data(data_store)
        
        return jsonify({
            'success': True,
            'message': f'ลบรูป {deleted_count} รูปจาก RAM เรียบร้อย'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/clear_all_photos', methods=['POST'])
def clear_all_photos():
    try:
        deleted_count = len(PHOTOS_IN_MEMORY)
        PHOTOS_IN_MEMORY.clear()
        
        data_store = load_data()
        data_store.update({
            'photos': [],
            'latest_qr': None,
            'stats': {
                'total_photos': 0,
                'total_downloads': 0,
                'total_sessions': 0,
                'retake_used': 0
            }
        })
        save_data(data_store)
        
        lock_status = load_lock_status()
        lock_status.update({
            'camera_locked': False,
            'locked_by_code': None,
            'retake_available': True
        })
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'message': f'ลบรูปทั้งหมด {deleted_count} รูปจาก RAM เรียบร้อย',
            'deleted_count': deleted_count
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/memory_status')
def memory_status():
    try:
        total_files = len(PHOTOS_IN_MEMORY)
        total_bytes = sum(len(photo_bytes) for photo_bytes in PHOTOS_IN_MEMORY.values())
        total_mb = total_bytes / (1024 * 1024)
        
        return jsonify({
            'total_files': total_files,
            'total_bytes': total_bytes,
            'total_mb': round(total_mb, 2),
            'filenames': list(PHOTOS_IN_MEMORY.keys())
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export_csv')
def export_csv():
    try:
        data_store = load_data()
        
        csv_content = "รหัส,จำนวนรูป,ดาวน์โหลด,เวลา,ถ่ายใหม่,กรอบรูป\n"
        for photo in data_store['photos']:
            has_template = 'ใช่' if photo.get('template_id') else 'ไม่ใช่'
            csv_content += f"{photo['pickup_code']},{len(photo['filenames'])},{photo['download_count']},{photo['time_display']},{'ใช่' if photo.get('retake_used') else 'ไม่ใช่'},{has_template}\n"
        
        response = Response(csv_content, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=photobooth_data.csv'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==================== ERROR HANDLERS ====================
@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Not Found', 'message': f'The requested URL {request.path} was not found'}), 404
    return render_template('error.html', error_code=404, error_message="ไม่พบหน้าที่คุณต้องการ"), 404

@app.errorhandler(500)
def server_error(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Internal Server Error', 'message': 'An internal server error occurred'}), 500
    return render_template('error.html', error_code=500, error_message="เกิดข้อผิดพลาดในเซิร์ฟเวอร์"), 500

# ==================== MAIN ====================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 21555))
    debug_mode = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # โหลด frame templates
    load_frame_templates()
    
    print("🚀 Starting Photo Booth Server")
    print("=" * 50)
    print(f"📌 Server URL: http://localhost:{port}")
    print(f"💾 Storage Mode: RAM (In-Memory)")
    print(f"🖼️ Frame System: Enabled")
    print(f"📸 Camera Page: http://localhost:{port}/capture")
    print(f"📱 QR Display: http://localhost:{port}/qr")
    print(f"⚙️ Admin Panel: http://localhost:{port}/admin")
    print(f"🔧 Debug Mode: {debug_mode}")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
