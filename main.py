import os
import json
import uuid
import qrcode
import base64
from datetime import datetime
from io import BytesIO

from flask import Flask, render_template, request, jsonify, send_file, redirect, url_for, send_from_directory, Response

# สร้าง Flask app
app = Flask(__name__, 
            static_folder='static',
            template_folder='templates')
app.secret_key = 'photo-booth-event-secret-key-2024'

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max
app.config['ALLOWED_EXTENSIONS'] = {'png', 'jpg', 'jpeg', 'gif'}
app.config['DATA_FILE'] = os.path.join(BASE_DIR, 'photobooth_data.json')
app.config['LOCK_FILE'] = os.path.join(BASE_DIR, 'camera_lock.json')

# *** เปลี่ยนจากบันทึกในดิสก์เป็นบันทึกใน RAM ***
# Dictionary เก็บรูปภาพใน memory (RAM)
# โครงสร้าง: {filename: bytes_data}
PHOTOS_IN_MEMORY = {}

# สร้างโฟลเดอร์สำหรับ static และ templates เท่านั้น (ไม่ต้องสร้างโฟลเดอร์ photos)
os.makedirs('static', exist_ok=True)
os.makedirs('templates', exist_ok=True)

# ฟังก์ชันจัดการข้อมูล
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

def generate_qr_code(url):
    """สร้าง QR Code จาก URL"""
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

# ==================== ROUTES ====================

@app.route('/')
def index():
    return redirect(url_for('capture'))

@app.route('/capture')
def capture():
    """หน้าเครื่องถ่ายรูป"""
    lock_status = load_lock_status()
    return render_template('capture.html', 
                          camera_locked=lock_status['camera_locked'],
                          retake_available=lock_status['retake_available'],
                          camera_enabled=lock_status['camera_enabled'])

@app.route('/qr')
def qr_display():
    """หน้าเครื่องแสดง QR"""
    return render_template('qr_display.html')

@app.route('/admin')
def admin():
    """หน้าแอดมิน"""
    data = load_data()
    lock_status = load_lock_status()
    # แปลง reversed iterator เป็น list
    reversed_photos = list(reversed(data['photos']))
    return render_template('admin.html', 
                          photos=reversed_photos, 
                          stats=data['stats'],
                          latest_qr=data.get('latest_qr'),
                          camera_locked=lock_status['camera_locked'],
                          locked_by_code=lock_status['locked_by_code'])


@app.route('/api/full_status')
def full_status():
    """API ตรวจสอบสถานะละเอียด"""
    data_store = load_data()
    lock_status = load_lock_status()
    
    return jsonify({
        'camera_locked': lock_status['camera_locked'],
        'locked_by_code': lock_status['locked_by_code'],
        'retake_available': lock_status['retake_available'],
        'camera_enabled': lock_status['camera_enabled'],
        'latest_qr_exists': data_store.get('latest_qr') is not None,
        'latest_qr_code': data_store.get('latest_qr', {}).get('code') if data_store.get('latest_qr') else None,
        'total_sessions': data_store['stats']['total_sessions'],
        'total_photos': data_store['stats']['total_photos'],
        'server_time': datetime.now().isoformat()
    })

@app.route('/scan/<code>')
def scan_code(code):
    """หน้า redirect สำหรับ QR scan"""
    data = load_data()
    for photo in data['photos']:
        if photo['pickup_code'] == code:
            data['stats']['total_downloads'] += 1
            save_data(data)
            
            # ปลดล็อคเครื่องถ่ายรูปเมื่อมีการสแกน
            lock_status = load_lock_status()
            if lock_status['locked_by_code'] == code:
                lock_status['camera_locked'] = False
                lock_status['locked_by_code'] = None
                lock_status['retake_available'] = True
                save_lock_status(lock_status)
                
                # ล้าง latest_qr ด้วย
                data['latest_qr'] = None
                save_data(data)
                
                # ส่ง event ไปยัง SSE หรือ WebSocket เพื่อแจ้งหน้า capture
                # (สำหรับเวอร์ชันพื้นฐาน ให้หน้า capture ตรวจสอบสถานะเป็นระยะๆ)
            
            return redirect(url_for('download', code=code))
    return render_template('scan.html', code=code, found=False)

@app.route('/download/<code>')
def download(code):
    """หน้ารับรูปบนมือถือ"""
    data = load_data()
    for photo in data['photos']:
        if photo['pickup_code'] == code:
            # สร้าง URL เต็มสำหรับรูปภาพ
            base_url = request.host_url.rstrip('/')
            return render_template('download.html', 
                                 photo_info=photo,
                                 base_url=base_url)
    return render_template('download.html', error="ไม่พบรหัสดังกล่าว")

@app.route('/favicon.ico')
def favicon():
    """ส่งคืน favicon เปล่าเพื่อป้องกัน 404 error"""
    # ส่งคืนไฟล์ PNG ขนาด 1x1 pixel สีใส
    transparent_icon = base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII='
    )
    return Response(transparent_icon, mimetype='image/png')

# ==================== APIs ====================

@app.route('/api/upload', methods=['POST'])
def upload_photo():
    """API สำหรับอัปโหลดรูปจากกล้อง - บันทึกใน RAM"""
    try:
        data = request.get_json()
        if not data or 'photos' not in data:
            return jsonify({'error': 'ไม่มีข้อมูลรูปภาพ'}), 400
        
        photos_data = data['photos']
        if not photos_data:
            return jsonify({'error': 'ไม่มีรูปภาพ'}), 400
        
        pickup_code = str(uuid.uuid4())[:8].upper()
        
        saved_files = []
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # *** บันทึกรูปใน RAM แทนการเขียนลงดิสก์ ***
        for i, photo_data in enumerate(photos_data):
            if ',' in photo_data:
                format, imgstr = photo_data.split(',', 1)
                try:
                    photo_bytes = base64.b64decode(imgstr)
                    
                    filename = f"{timestamp}_{pickup_code}_{i+1}.png"
                    
                    # บันทึกลง RAM (dictionary) แทนการเขียนไฟล์
                    PHOTOS_IN_MEMORY[filename] = photo_bytes
                    saved_files.append(filename)
                    
                    print(f"✅ Saved to RAM: {filename} ({len(photo_bytes)} bytes)")
                except Exception as e:
                    print(f"❌ Error saving photo {i} to RAM: {e}")
                    continue
        
        if not saved_files:
            return jsonify({'error': 'ไม่สามารถบันทึกรูปได้'}), 500
        
        # สร้าง QR Code
        base_url = request.host_url.rstrip('/')
        qr_url = f"{base_url}/scan/{pickup_code}"
        qr_code = generate_qr_code(qr_url)
        
        # บันทึกข้อมูล
        data_store = load_data()
        
        photo_info = {
            'id': str(uuid.uuid4()),
            'pickup_code': pickup_code,
            'filenames': saved_files,
            'timestamp': datetime.now().isoformat(),
            'time_display': datetime.now().strftime('%H:%M:%S %d/%m/%Y'),
            'qr_url': qr_url,
            'download_count': 0,
            'retake_used': False
        }
        
        data_store['photos'].append(photo_info)
        data_store['stats']['total_photos'] += len(saved_files)
        data_store['stats']['total_sessions'] += 1
        
        # อัปเดต latest_qr
        data_store['latest_qr'] = {
            'code': pickup_code,
            'qr_image': qr_code,
            'timestamp': photo_info['timestamp'],
            'url': qr_url,
            'time_display': photo_info['time_display']
        }
        
        save_data(data_store)
        
        # ล็อคเครื่องถ่ายรูป
        lock_status = load_lock_status()
        lock_status['camera_locked'] = True
        lock_status['locked_by_code'] = pickup_code
        lock_status['locked_at'] = datetime.now().isoformat()
        save_lock_status(lock_status)
        
        print(f"📸 Photos saved in RAM - Total in memory: {len(PHOTOS_IN_MEMORY)} files")
        
        return jsonify({
            'success': True,
            'pickup_code': pickup_code,
            'qr_code': qr_code,
            'files_saved': len(saved_files),
            'storage': 'RAM'
        })
    except Exception as e:
        print(f"Error in upload_photo: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/photos/<filename>')
def get_photo(filename):
    """ส่งรูปจาก RAM แทนการอ่านจากดิสก์"""
    try:
        # ดึงรูปจาก RAM
        if filename in PHOTOS_IN_MEMORY:
            photo_bytes = PHOTOS_IN_MEMORY[filename]
            return send_file(
                BytesIO(photo_bytes),
                mimetype='image/png',
                as_attachment=False,
                download_name=filename
            )
        else:
            print(f"❌ Photo not found in RAM: {filename}")
            return jsonify({'error': 'ไม่พบรูปภาพ'}), 404
    except Exception as e:
        print(f"Error serving photo from RAM: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/download_all/<code>')
def download_all(code):
    """ดาวน์โหลดรูปทั้งหมดเป็น ZIP จาก RAM"""
    try:
        import zipfile
        
        data = load_data()
        photo_info = None
        
        for photo in data['photos']:
            if photo['pickup_code'] == code:
                photo_info = photo
                break
        
        if not photo_info:
            return jsonify({'error': 'ไม่พบรหัสดังกล่าว'}), 404
        
        # สร้าง ZIP file ใน memory
        zip_buffer = BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for i, filename in enumerate(photo_info['filenames']):
                if filename in PHOTOS_IN_MEMORY:
                    photo_bytes = PHOTOS_IN_MEMORY[filename]
                    # ใส่ไฟล์ลง ZIP
                    zip_file.writestr(f'photo_{i+1}.png', photo_bytes)
        
        zip_buffer.seek(0)
        
        # นับการดาวน์โหลด
        data['stats']['total_downloads'] += 1
        save_data(data)
        
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=f'photobooth_{code}.zip'
        )
    except Exception as e:
        print(f"Error creating ZIP from RAM: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/retake', methods=['POST'])
def retake_photo():
    """API สำหรับถ่ายรูปใหม่"""
    try:
        lock_status = load_lock_status()
        
        if not lock_status['retake_available']:
            return jsonify({'error': 'ถ่ายใหม่ได้แค่ 1 ครั้งเท่านั้น'}), 403
        
        if not lock_status['camera_locked']:
            return jsonify({'error': 'ไม่มีเซสชันที่ต้องถ่ายใหม่'}), 400
        
        # ทำเครื่องหมายว่าใช้สิทธิ์ถ่ายใหม่แล้ว
        data_store = load_data()
        if lock_status['locked_by_code']:
            for photo in data_store['photos']:
                if photo['pickup_code'] == lock_status['locked_by_code']:
                    photo['retake_used'] = True
                    data_store['stats']['retake_used'] += 1
                    break
        
        save_data(data_store)
        
        # ปลดล็อคเครื่องถ่ายรูป แต่ไม่อนุญาตให้ถ่ายใหม่อีก
        lock_status['camera_locked'] = False
        lock_status['retake_available'] = False
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'message': 'พร้อมถ่ายรูปใหม่ (ครั้งสุดท้าย)'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/latest_qr')
def latest_qr():
    """API ดึง QR Code ล่าสุด"""
    try:
        data = load_data()
        latest = data.get('latest_qr')
        
        if latest:
            return jsonify(latest)
        else:
            return jsonify(None)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/lock_status')
def lock_status():
    """API ตรวจสอบสถานะการล็อค"""
    try:
        status = load_lock_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/unlock_camera', methods=['POST'])
def unlock_camera():
    """API ปลดล็อคกล้อง (สำหรับแอดมิน)"""
    try:
        lock_status = load_lock_status()
        lock_status['camera_locked'] = False
        lock_status['locked_by_code'] = None
        lock_status['retake_available'] = True
        save_lock_status(lock_status)
        
        # ล้าง latest_qr
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
    """API เปิด/ปิดกล้อง"""
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

@app.route('/api/delete_photo/<photo_id>', methods=['DELETE'])
def delete_photo(photo_id):
    """API ลบรูป 1 เซสชัน จาก RAM"""
    try:
        data_store = load_data()
        
        # หารูปที่จะลบ
        photo_to_delete = None
        for i, photo in enumerate(data_store['photos']):
            if photo['id'] == photo_id:
                photo_to_delete = photo
                data_store['photos'].pop(i)
                break
        
        if not photo_to_delete:
            return jsonify({'error': 'ไม่พบรูป'}), 404
        
        # ลบรูปจาก RAM
        deleted_count = 0
        for filename in photo_to_delete['filenames']:
            if filename in PHOTOS_IN_MEMORY:
                del PHOTOS_IN_MEMORY[filename]
                deleted_count += 1
                print(f"🗑️ Deleted from RAM: {filename}")
        
        # อัปเดตสถิติ
        data_store['stats']['total_photos'] -= len(photo_to_delete['filenames'])
        data_store['stats']['total_sessions'] -= 1
        data_store['stats']['total_downloads'] -= photo_to_delete.get('download_count', 0)
        
        save_data(data_store)
        
        print(f"📊 Remaining in RAM: {len(PHOTOS_IN_MEMORY)} files")
        
        return jsonify({
            'success': True,
            'message': f'ลบรูป {deleted_count} รูปจาก RAM เรียบร้อย'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/generate_sample', methods=['POST'])
def generate_sample():
    """API สร้างข้อมูลตัวอย่าง (ไม่มีรูปจริงใน RAM)"""
    try:
        data_store = load_data()
        
        # สร้างข้อมูลตัวอย่าง (แต่ไม่มีรูปใน RAM)
        sample_photos = [
            {
                'id': str(uuid.uuid4()),
                'pickup_code': 'SAMPLE1',
                'filenames': ['sample1.png', 'sample2.png'],
                'timestamp': datetime.now().isoformat(),
                'time_display': datetime.now().strftime('%H:%M:%S %d/%m/%Y'),
                'qr_url': f"{request.host_url.rstrip('/')}/scan/SAMPLE1",
                'download_count': 0,
                'retake_used': False
            },
            {
                'id': str(uuid.uuid4()),
                'pickup_code': 'SAMPLE2',
                'filenames': ['sample3.png'],
                'timestamp': datetime.now().isoformat(),
                'time_display': datetime.now().strftime('%H:%M:%S %d/%m/%Y'),
                'qr_url': f"{request.host_url.rstrip('/')}/scan/SAMPLE2",
                'download_count': 1,
                'retake_used': True
            }
        ]
        
        # เพิ่มข้อมูลตัวอย่าง
        for photo in sample_photos:
            data_store['photos'].append(photo)
            data_store['stats']['total_photos'] += len(photo['filenames'])
            data_store['stats']['total_sessions'] += 1
            data_store['stats']['total_downloads'] += photo['download_count']
        
        # สร้าง QR ล่าสุด
        if sample_photos:
            latest = sample_photos[-1]
            data_store['latest_qr'] = {
                'code': latest['pickup_code'],
                'qr_image': generate_qr_code(latest['qr_url']),
                'timestamp': latest['timestamp'],
                'url': latest['qr_url'],
                'time_display': latest['time_display']
            }
        
        save_data(data_store)
        
        return jsonify({
            'success': True,
            'message': 'สร้างข้อมูลตัวอย่างสำเร็จ (ไม่มีรูปจริงใน RAM)',
            'added_photos': len(sample_photos)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/clear_all_photos', methods=['POST'])
def clear_all_photos():
    """API ลบรูปทั้งหมดจาก RAM"""
    try:
        data_store = load_data()
        
        # นับจำนวนรูปที่จะลบ
        deleted_count = len(PHOTOS_IN_MEMORY)
        
        # ล้างรูปทั้งหมดจาก RAM
        PHOTOS_IN_MEMORY.clear()
        print(f"🗑️ Cleared all photos from RAM ({deleted_count} files)")
        
        # รีเซ็ตข้อมูล
        data_store['photos'] = []
        data_store['latest_qr'] = None
        data_store['stats'] = {
            'total_photos': 0,
            'total_downloads': 0,
            'total_sessions': 0,
            'retake_used': 0
        }
        
        save_data(data_store)
        
        # รีเซ็ตสถานะล็อค
        lock_status = load_lock_status()
        lock_status['camera_locked'] = False
        lock_status['locked_by_code'] = None
        lock_status['retake_available'] = True
        save_lock_status(lock_status)
        
        return jsonify({
            'success': True,
            'message': f'ลบรูปทั้งหมด {deleted_count} รูปจาก RAM เรียบร้อย',
            'deleted_count': deleted_count
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export_csv')
def export_csv():
    """API Export ข้อมูลเป็น CSV"""
    try:
        data_store = load_data()
        
        # สร้าง CSV content
        csv_content = "รหัส,จำนวนรูป,ดาวน์โหลด,เวลา,ถ่ายใหม่\n"
        for photo in data_store['photos']:
            csv_content += f"{photo['pickup_code']},{len(photo['filenames'])},{photo['download_count']},{photo['time_display']},{'ใช่' if photo.get('retake_used') else 'ไม่ใช่'}\n"
        
        # ส่งคืนเป็นไฟล์ CSV
        response = Response(csv_content, mimetype='text/csv')
        response.headers['Content-Disposition'] = 'attachment; filename=photobooth_data.csv'
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/save_settings', methods=['POST'])
def save_settings():
    """API บันทึกการตั้งค่าระบบ"""
    try:
        data = request.get_json()
        # ในเวอร์ชันนี้แค่คืนค่า success
        # สามารถขยายได้ในอนาคต
        return jsonify({
            'success': True,
            'message': 'บันทึกการตั้งค่าเรียบร้อย',
            'settings': data
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/emergency_unlock', methods=['POST'])
def emergency_unlock():
    """API ปลดล็อคฉุกเฉิน"""
    try:
        # ปลดล็อคเครื่องถ่ายรูป
        lock_status = load_lock_status()
        lock_status['camera_locked'] = False
        lock_status['locked_by_code'] = None
        lock_status['retake_available'] = True
        save_lock_status(lock_status)
        
        # ล้าง QR ล่าสุด
        data_store = load_data()
        data_store['latest_qr'] = None
        save_data(data_store)
        
        return jsonify({
            'success': True,
            'message': 'ปลดล็อคฉุกเฉินสำเร็จ'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/memory_status')
def memory_status():
    """API ตรวจสอบสถานะ RAM"""
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

# ==================== ERROR HANDLERS ====================

@app.errorhandler(404)
def not_found(e):
    """Handle 404 errors"""
    # ตรวจสอบว่าเป็น API request หรือไม่
    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'Not Found',
            'message': f'The requested URL {request.path} was not found',
            'code': 404
        }), 404
    
    # สำหรับหน้าเว็บปกติ
    return render_template('error.html', 
                         error_code=404,
                         error_message="ไม่พบหน้าที่คุณต้องการ"), 404

@app.errorhandler(500)
def server_error(e):
    """Handle 500 errors"""
    # ตรวจสอบว่าเป็น API request หรือไม่
    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'Internal Server Error',
            'message': 'An internal server error occurred',
            'code': 500
        }), 500
    
    # สำหรับหน้าเว็บปกติ
    return render_template('error.html',
                         error_code=500,
                         error_message="เกิดข้อผิดพลาดในเซิร์ฟเวอร์"), 500

@app.errorhandler(400)
def bad_request(e):
    """Handle 400 errors"""
    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'Bad Request',
            'message': 'The request could not be understood',
            'code': 400
        }), 400
    
    return render_template('error.html',
                         error_code=400,
                         error_message="คำขอไม่ถูกต้อง"), 400

@app.errorhandler(403)
def forbidden(e):
    """Handle 403 errors"""
    if request.path.startswith('/api/'):
        return jsonify({
            'error': 'Forbidden',
            'message': 'You do not have permission to access this resource',
            'code': 403
        }), 403
    
    return render_template('error.html',
                         error_code=403,
                         error_message="คุณไม่มีสิทธิ์เข้าถึงหน้านี้"), 403

# ==================== MAIN ====================

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 21555))
    debug_mode = os.environ.get('DEBUG', 'True').lower() == 'true'
    
    # ใช้ SSL สำหรับ localhost
    ssl_context = None
    if debug_mode:
        # พยายามหา certificate ที่สร้างจาก mkcert
        cert_path = os.path.join(BASE_DIR, 'localhost+1.pem')
        key_path = os.path.join(BASE_DIR, 'localhost+1-key.pem')
        
        if os.path.exists(cert_path) and os.path.exists(key_path):
            ssl_context = (cert_path, key_path)
            print(f"🔐 Using SSL with certificate: {cert_path}")
    
    print(f"🚀 Starting Photo Booth Server on port {port}")
    print(f"💾 Storage Mode: RAM (In-Memory)")
    print(f"📸 Camera Page: https://localhost:{port}/capture")
    print(f"📱 QR Display: https://localhost:{port}/qr")
    print(f"⚙️ Admin Panel: https://localhost:{port}/admin")
    print(f"🔧 Debug Mode: {debug_mode}")
    
    app.run(
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        ssl_context=ssl_context
    )