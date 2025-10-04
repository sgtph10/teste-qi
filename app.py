from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import uuid
import json
import qrcode
import io
import base64
import mercadopago
import sqlite3
from datetime import datetime, timedelta
import threading
import time
from dotenv import load_dotenv
import tempfile

# Carregar variáveis de ambiente
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'your-secret-key-here')
CORS(app)

# Configuração do banco de dados para Render
if os.getenv('RENDER'):
    # No Render, usar diretório temporário
    DB_PATH = os.path.join(tempfile.gettempdir(), 'qi_test.db')
    print(f"🔧 Ambiente Render detectado - DB Path: {DB_PATH}")
else:
    DB_PATH = 'qi_test.db'
    print(f"🔧 Ambiente local - DB Path: {DB_PATH}")

# Configurações do Mercado Pago (PRODUÇÃO)
MP_ACCESS_TOKEN = os.getenv('MP_ACCESS_TOKEN')
if not MP_ACCESS_TOKEN:
    raise ValueError("MP_ACCESS_TOKEN não encontrado no arquivo .env")

if MP_ACCESS_TOKEN.startswith('TEST-'):
    print("⚠️  AVISO: Usando token de TESTE. Para produção, use token de PRODUÇÃO!")

sdk = mercadopago.SDK(MP_ACCESS_TOKEN)

def get_db_connection():
    """Função para obter conexão com o banco de dados"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")  # Melhor performance
        return conn
    except Exception as e:
        print(f"❌ Erro ao conectar ao banco: {e}")
        raise

# Configuração do banco de dados SQLite
def init_db():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Tabela para armazenar testes
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uuid TEXT UNIQUE NOT NULL,
                user_answers TEXT NOT NULL,
                score INTEGER NOT NULL,
                level TEXT NOT NULL,
                correct_answers INTEGER NOT NULL,
                percentage REAL NOT NULL,
                payment_id TEXT,
                payment_status TEXT DEFAULT 'pending',
                qr_code_data TEXT,
                customer_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP
            )
        ''')

        # Tabela para logs de webhook
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS webhook_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_id TEXT,
                status TEXT,
                data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()
        print("✅ Banco de dados inicializado com sucesso")
    except Exception as e:
        print(f"❌ Erro ao inicializar banco: {e}")
        raise

# Inicializar banco
try:
    init_db()
except Exception as e:
    print(f"❌ ERRO CRÍTICO: Falha ao inicializar banco de dados: {e}")

# Limpeza automática de testes expirados
def cleanup_expired_tests():
    while True:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                'DELETE FROM tests WHERE expires_at < ? AND payment_status = "pending"',
                (datetime.now(),))
            deleted = cursor.rowcount
            if deleted > 0:
                print(f"🧹 Limpeza: {deleted} testes expirados removidos")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Erro na limpeza: {e}")

        # Executar a cada hora
        time.sleep(3600)

# Iniciar thread de limpeza
cleanup_thread = threading.Thread(target=cleanup_expired_tests, daemon=True)
cleanup_thread.start()

@app.route('/')
def index():
    try:
        # Tentar encontrar o arquivo index.html
        possible_paths = ['index.html', './index.html', 'templates/index.html']
        
        for path in possible_paths:
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    return f.read()
            except FileNotFoundError:
                continue
        
        # Se não encontrar o arquivo, retornar uma página simples
        return """
        <!DOCTYPE html>
        <html>
        <head><title>QI Test</title></head>
        <body>
        <h1>QI Test API</h1>
        <p>API está funcionando! Arquivo index.html não encontrado.</p>
        <a href="/health">Verificar saúde da API</a>
        </body>
        </html>
        """
    except Exception as e:
        print(f"❌ Erro ao carregar index.html: {e}")
        return jsonify({"error": "Erro ao carregar página"}), 500

@app.route('/submit_test', methods=['POST'])
def submit_test():
    """Recebe as respostas do teste e calcula a pontuação"""
    try:
        print("📥 Recebendo dados do teste...")
        
        # Verificar se há dados JSON
        if not request.is_json:
            print("❌ Request não é JSON")
            return jsonify({"error": "Content-Type deve ser application/json"}), 400
        
        data = request.get_json()
        if not data:
            print("❌ Dados JSON vazios")
            return jsonify({"error": "Dados JSON inválidos ou vazios"}), 400
        
        print(f"📊 Dados recebidos: {data}")
        
        user_answers = data.get('answers', [])
        customer_email = data.get('email', 'cliente@qi-test.com.br')
        
        print(f"📝 Respostas: {len(user_answers)} itens")
        print(f"📧 Email: {customer_email}")
        
        # Validação do número de respostas
        if len(user_answers) != 30:
            error_msg = f"Número incorreto de respostas: {len(user_answers)}/30"
            print(f"❌ {error_msg}")
            return jsonify({"error": error_msg}), 400
        
        # Validação dos tipos de dados das respostas
        for i, answer in enumerate(user_answers):
            if not isinstance(answer, int) or answer < 0 or answer > 4:
                error_msg = f"Resposta inválida na posição {i}: {answer} (deve ser inteiro entre 0-4)"
                print(f"❌ {error_msg}")
                return jsonify({"error": error_msg}), 400

        # Respostas corretas (as mesmas do frontend)
        correct_answers_list = [1, 2, 3, 1, 4, 3, 2, 0, 1, 1, 1, 1, 1, 2, 2,
                                4, 2, 1, 1, 1, 0, 0, 3, 1, 1, 1, 1, 3, 0, 0]

        # Calcular pontuação
        correct_count = 0
        for i, answer in enumerate(user_answers):
            if i < len(correct_answers_list) and answer == correct_answers_list[i]:
                correct_count += 1

        percentage = (correct_count / 30) * 100
        print(f"🎯 Acertos: {correct_count}/30 ({percentage:.1f}%)")

        # Calcular QI (fórmula refinada para produção)
        if percentage >= 95:
            iq_score = int(145 + (percentage - 95) * 2)
        elif percentage >= 85:
            iq_score = int(130 + (percentage - 85) * 1.5)
        elif percentage >= 70:
            iq_score = int(115 + (percentage - 70) * 1)
        elif percentage >= 50:
            iq_score = int(100 + (percentage - 50) * 0.75)
        elif percentage >= 30:
            iq_score = int(85 + (percentage - 30) * 0.75)
        else:
            iq_score = int(70 + percentage * 0.5)

        # Limitar QI entre 50 e 200
        iq_score = max(50, min(200, iq_score))

        # Determinar nível
        if iq_score >= 140:
            level = "Gênio"
        elif iq_score >= 130:
            level = "Superdotado"
        elif iq_score >= 115:
            level = "Acima da Média"
        elif iq_score >= 85:
            level = "Média"
        else:
            level = "Abaixo da Média"

        # Gerar UUID único
        test_uuid = str(uuid.uuid4())
        print(f"🆔 UUID gerado: {test_uuid}")

        # Salvar no banco (com melhor tratamento de erro)
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            expires_at = datetime.now() + timedelta(hours=24)

            cursor.execute('''
                INSERT INTO tests (uuid, user_answers, score, level,
                                   correct_answers, percentage, customer_email,
                                   expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (test_uuid, json.dumps(user_answers), iq_score, level,
                  correct_count, percentage, customer_email, expires_at))

            conn.commit()
            conn.close()
            
            print(f"✅ Teste salvo no banco: {test_uuid} - QI: {iq_score} - Level: {level}")

        except sqlite3.Error as db_error:
            print(f"❌ Erro do banco de dados: {db_error}")
            return jsonify({"error": "Erro ao salvar no banco de dados"}), 500

        return jsonify({
            'success': True,
            'test_uuid': test_uuid,
            'score': iq_score,
            'level': level,
            'correct_answers': correct_count,
            'percentage': round(percentage, 1)
        })

    except Exception as e:
        print(f"❌ Erro inesperado ao processar teste: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Erro interno do servidor: {str(e)}"}), 500

@app.route('/create_payment', methods=['POST'])
def create_payment():
    """Cria um pagamento PIX via Mercado Pago - PRODUÇÃO"""
    try:
        print("💳 Iniciando criação de pagamento...")
        
        data = request.get_json()
        if not data:
            return jsonify({"error": "Dados JSON inválidos"}), 400
            
        test_uuid = data.get('test_uuid')
        print(f"🆔 Test UUID: {test_uuid}")

        if not test_uuid:
            return jsonify({"error": "test_uuid é obrigatório"}), 400

        # Verificar se o teste existe
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM tests WHERE uuid = ?', (test_uuid,))
            test_data = cursor.fetchone()

            if not test_data:
                conn.close()
                print(f"❌ Teste não encontrado: {test_uuid}")
                return jsonify({"error": "Teste não encontrado"}), 404
                
            print(f"✅ Teste encontrado no banco")

        except sqlite3.Error as db_error:
            print(f"❌ Erro ao buscar teste: {db_error}")
            return jsonify({"error": "Erro ao acessar banco de dados"}), 500

        # URL base para webhook (Render)
        base_url = os.getenv('BASE_URL', 'https://teste-de-inteligencia.onrender.com')
        if 'localhost' in request.host_url or '127.0.0.1' in request.host_url:
            base_url = request.host_url.rstrip('/')

        print(f"🔗 Base URL: {base_url}")

        # Criar pagamento no Mercado Pago (PRODUÇÃO)
        payment_data = {
            "transaction_amount": 5.29,
            "description": "Teste de QI - Resultado Completo - QI Test Pro",
            "payment_method_id": "pix",
            "payer": {
                "email": test_data[10] if test_data[10] else "cliente@qi-test.com.br",
                "first_name": "Cliente",
                "last_name": "QI"
            },
            "external_reference": test_uuid,
            "notification_url": f"{base_url}/webhook/mercadopago",
            "date_of_expiration": (datetime.now() + timedelta(hours=2)).isoformat(),
            "metadata": {
                "test_uuid": test_uuid,
                "integration": "qi_test_render"
            }
        }

        print(f"💳 Dados do pagamento: {payment_data}")
        print(f"🔗 Webhook URL: {base_url}/webhook/mercadopago")

        try:
            payment_response = sdk.payment().create(payment_data)
            print(f"📤 Resposta do Mercado Pago: {payment_response}")

        except Exception as mp_error:
            print(f"❌ Erro na API do Mercado Pago: {mp_error}")
            conn.close()
            return jsonify({"error": f"Erro na API do Mercado Pago: {str(mp_error)}"}), 500

        if payment_response["status"] != 201:
            conn.close()
            print(f"❌ Erro MP - Status: {payment_response['status']}")
            return jsonify({
                "error": "Erro ao criar pagamento",
                "details": payment_response.get("response", {}),
                "status": payment_response.get("status")
            }), 500

        payment = payment_response["response"]
        payment_id = payment["id"]
        print(f"✅ Pagamento criado - ID: {payment_id}")

        # Obter dados do PIX
        pix_data = payment.get("point_of_interaction", {}).get("transaction_data", {})
        qr_code_text = pix_data.get("qr_code", "")
        qr_code_base64 = pix_data.get("qr_code_base64", "")

        print(f"🏦 PIX - Tem QR Code: {bool(qr_code_text)}, Tem Base64: {bool(qr_code_base64)}")

        # Se não tiver QR code base64, gerar um
        if not qr_code_base64 and qr_code_text:
            try:
                qr = qrcode.QRCode(version=1, box_size=10, border=5)
                qr.add_data(qr_code_text)
                qr.make(fit=True)

                img = qr.make_image(fill_color="black", back_color="white")
                img_buffer = io.BytesIO()
                img.save(img_buffer, format='PNG')
                qr_code_base64 = base64.b64encode(img_buffer.getvalue()).decode()
                print("✅ QR Code gerado localmente")
            except Exception as qr_error:
                print(f"❌ Erro ao gerar QR Code: {qr_error}")

        # Atualizar teste com dados do pagamento
        try:
            cursor.execute('''
                UPDATE tests
                SET payment_id = ?, qr_code_data = ?
                WHERE uuid = ?
            ''', (payment_id, qr_code_base64, test_uuid))

            conn.commit()
            conn.close()
            print("✅ Dados do pagamento salvos no banco")

        except sqlite3.Error as db_error:
            print(f"❌ Erro ao salvar dados do pagamento: {db_error}")
            conn.close()
            return jsonify({"error": "Erro ao salvar dados do pagamento"}), 500

        return jsonify({
            'success': True,
            'payment_id': payment_id,
            'qr_code_base64': qr_code_base64,
            'qr_code_text': qr_code_text,
            'expiration_time': 7200  # 2 horas em segundos
        })

    except Exception as e:
        print(f"❌ Erro inesperado ao criar pagamento: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Erro interno: {str(e)}"}), 500

@app.route('/webhook/mercadopago', methods=['POST'])
def mercadopago_webhook():
    """Webhook para receber notificações do Mercado Pago - PRODUÇÃO"""
    try:
        data = request.get_json()
        print(f"🔔 Webhook recebido: {data}")

        if not data:
            print("❌ Webhook sem dados")
            return jsonify({"status": "ok"}), 200

        # Log do webhook
        try:
            conn = get_db_connection()
            cursor = conn.cursor()

            payment_id = None
            if data.get('data', {}).get('id'):
                payment_id = data.get('data', {}).get('id')

            cursor.execute('''
                INSERT INTO webhook_logs (payment_id, status, data)
                VALUES (?, ?, ?)
            ''', (payment_id, data.get('action', ''), json.dumps(data)))

            # Verificar se é uma notificação de pagamento
            if (data.get('action') == 'payment.updated' or data.get('type') == 'payment'):
                payment_id = data.get('data', {}).get('id')

                if payment_id:
                    print(f"💳 Verificando pagamento ID: {payment_id}")

                    try:
                        # Buscar detalhes do pagamento
                        payment_info = sdk.payment().get(payment_id)

                        if payment_info["status"] == 200:
                            payment = payment_info["response"]
                            external_reference = payment.get("external_reference")
                            payment_status = payment.get("status")

                            print(f"💰 Payment ID: {payment_id}, Status: {payment_status}, Reference: {external_reference}")

                            if external_reference and payment_status in ['approved', 'authorized']:
                                # Atualizar status do teste
                                cursor.execute('''
                                    UPDATE tests
                                    SET payment_status = 'approved'
                                    WHERE uuid = ?
                                ''', (external_reference,))

                                if cursor.rowcount > 0:
                                    print(f"✅ PAGAMENTO APROVADO para teste: {external_reference}")
                                else:
                                    print(f"⚠️  Teste não encontrado para UUID: {external_reference}")

                            elif external_reference and payment_status in ['rejected', 'cancelled']:
                                cursor.execute('''
                                    UPDATE tests
                                    SET payment_status = 'rejected'
                                    WHERE uuid = ?
                                ''', (external_reference,))
                                print(f"❌ Pagamento rejeitado para teste: {external_reference}")

                    except Exception as mp_error:
                        print(f"❌ Erro ao buscar detalhes do pagamento: {mp_error}")

            conn.commit()
            conn.close()

        except sqlite3.Error as db_error:
            print(f"❌ Erro de banco no webhook: {db_error}")

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ Erro no webhook: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/check_payment/<test_uuid>', methods=['GET'])
def check_payment(test_uuid):
    """Verifica o status do pagamento para um teste"""
    try:
        print(f"🔍 Verificando pagamento para UUID: {test_uuid}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tests WHERE uuid = ?', (test_uuid,))
        test_data = cursor.fetchone()
        conn.close()

        if not test_data:
            print(f"❌ Teste não encontrado: {test_uuid}")
            return jsonify({"error": "Teste não encontrado"}), 404

        # Mapear colunas
        columns = ['id', 'uuid', 'user_answers', 'score', 'level',
                   'correct_answers', 'percentage', 'payment_id',
                   'payment_status', 'qr_code_data', 'customer_email',
                   'created_at', 'expires_at']
        test_dict = dict(zip(columns, test_data))

        print(f"💰 Status do pagamento: {test_dict['payment_status']}")

        return jsonify({
            'test_uuid': test_dict['uuid'],
            'payment_status': test_dict['payment_status'],
            'score': test_dict['score'],
            'level': test_dict['level'],
            'correct_answers': test_dict['correct_answers'],
            'percentage': test_dict['percentage'],
            'user_answers': json.loads(test_dict['user_answers']) if test_dict['user_answers'] else []
        })

    except Exception as e:
        print(f"❌ Erro ao verificar pagamento: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_result/<test_uuid>', methods=['GET'])
def get_result(test_uuid):
    """Retorna o resultado completo se o pagamento foi aprovado"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tests WHERE uuid = ?', (test_uuid,))
        test_data = cursor.fetchone()
        conn.close()

        if not test_data:
            return jsonify({"error": "Teste não encontrado"}), 404

        # Mapear colunas
        columns = ['id', 'uuid', 'user_answers', 'score', 'level',
                   'correct_answers', 'percentage', 'payment_id',
                   'payment_status', 'qr_code_data', 'customer_email',
                   'created_at', 'expires_at']
        test_dict = dict(zip(columns, test_data))

        if test_dict['payment_status'] != 'approved':
            return jsonify({
                "error": "Pagamento não aprovado",
                "payment_status": test_dict['payment_status']
            }), 403

        print(f"✅ Resultado liberado para teste: {test_uuid}")

        return jsonify({
            'success': True,
            'test_uuid': test_dict['uuid'],
            'score': test_dict['score'],
            'level': test_dict['level'],
            'correct_answers': test_dict['correct_answers'],
            'percentage': test_dict['percentage'],
            'user_answers': json.loads(test_dict['user_answers']) if test_dict['user_answers'] else [],
            'payment_status': test_dict['payment_status']
        })

    except Exception as e:
        print(f"❌ Erro ao obter resultado: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Endpoint de saúde da aplicação"""
    try:
        # Testar conexão com banco
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM tests')
        test_count = cursor.fetchone()[0]
        conn.close()
        db_status = "ok"
    except Exception as e:
        print(f"❌ Erro no health check do banco: {e}")
        db_status = f"error: {str(e)}"
        test_count = -1

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.1.0-production",
        "environment": os.getenv('FLASK_ENV', 'development'),
        "database": {
            "status": db_status,
            "path": DB_PATH,
            "test_count": test_count
        },
        "mercadopago": {
            "token_configured": bool(MP_ACCESS_TOKEN),
            "token_type": "PRODUCTION" if not MP_ACCESS_TOKEN.startswith('TEST-') else "TEST"
        }
    })

@app.route('/stats', methods=['GET'])
def stats():
    """Estatísticas básicas do sistema"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Total de testes
        cursor.execute('SELECT COUNT(*) FROM tests')
        total_tests = cursor.fetchone()[0]

        # Testes pagos
        cursor.execute('SELECT COUNT(*) FROM tests WHERE payment_status = "approved"')
        paid_tests = cursor.fetchone()[0]

        # Média de QI
        cursor.execute('SELECT AVG(score) FROM tests WHERE payment_status = "approved"')
        avg_qi = cursor.fetchone()[0] or 0

        conn.close()

        return jsonify({
            "total_tests": total_tests,
            "paid_tests": paid_tests,
            "avg_qi": round(avg_qi, 1),
            "conversion_rate": round((paid_tests / max(total_tests, 1)) * 100, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

    print("🚀 Servidor Flask PRODUÇÃO iniciado!")
    print(f"🔗 Porta: {port}")
    print(f"🔐 Debug: {debug}")
    print(f"🗄️  Database Path: {DB_PATH}")
    print("💳 Usando Mercado Pago PRODUÇÃO")

    app.run(debug=debug, host='0.0.0.0', port=port)
