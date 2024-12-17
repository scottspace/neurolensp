from flask import Flask, request, jsonify, render_template, redirect, session
from flask import stream_with_context, Response, make_response, send_file, url_for
from firestore_session import FirestoreSessionInterface, FirestoreSession
from google.oauth2 import id_token
from google.auth.transport import requests
from oauthlib.oauth2 import WebApplicationClient
from datetime import datetime, timezone
from urllib.parse import urlparse
from tzlocal import get_localzone
from PIL import Image, ImageOps,PngImagePlugin
from pillow_heif import register_heif_opener
import json
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
) 

register_heif_opener() # add HEIF support

import datetime
import os
import zipfile
import hashlib
from flask_cors import CORS
from google.cloud import storage
import os
import replicate
import mimetypes
import requests as webrequests
import uuid

import base64
from cryptography.fernet import Fernet
import urllib.parse

import firebase_admin
from firebase_admin import firestore, get_app

import time

# Internal imports
from user import User

# Ensure you set your client ID here
CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
REPLICATE_USER = os.getenv('REPLICATE_USER')
GOOGLE_DISCOVERY_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)
UPLOAD_FOLDER = '/tmp/uploads/'
PROCESSED_FOLDER = '/tmp/processed/'
ZIP_FOLDER = '/tmp/zipped/'

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ZIP_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

# Initialize the GCS client
storage_client = storage.Client()
bucket_name = os.getenv("BUCKET_NAME")

# Flask app setup
app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app)

# oauth2 setup
client = WebApplicationClient(CLIENT_ID)

# Google OAuth endpoints
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

#firestore setup
db = firestore.client()

# Initialize Firebase session interface
# Replace 'your-firebase-database-url' and 'serviceAccountKey.json' with your actual values
app.session_interface = FirestoreSessionInterface()

# User session management setup
# https://flask-login.readthedocs.io/en/latest
login_manager = LoginManager()
login_manager.init_app(app)

# Flask-Login helper to retrieve a user from our db
@login_manager.user_loader
def load_user(user_id):
    return User.get(user_id)

print("Main.py 1")

#
## Auth
#

def auth_user(user_id, name, email, profile_pic):
    # Create a user in your db with the information provided
    # by Google
    user = User(id_=user_id, name=name, email=email, profile_pic=profile_pic)

    # Doesn't exist? Add it to the database.
    if not User.get(user_id):
        User.create(user_id, name, email, profile_pic)

    # Begin user session by logging the user in
    login_user(user)
    
    # remember userid
    session['user_id'] = user_id
    
    print("Logged in user")

@app.route('/auth/google', methods=['POST'])
def auth_google():
    # Get Google's authorization endpoint from the discovery document
    google_provider_cfg = webrequests.get(GOOGLE_DISCOVERY_URL).json()
    authorization_endpoint = google_provider_cfg["authorization_endpoint"]
    
    # Prepare the redirect URI for the callback
    request_uri = client.prepare_request_uri(
        authorization_endpoint,
        redirect_uri=request.url_root + "auth/google/callback",
        scope=["openid", "email", "profile"],
    )
    
    print(f"Google auth redirecting to {request_uri}")
    
    # Redirect to Google OAuth 2.0 for login
    return redirect(request_uri)

#proposed by chatgpt - didn't work
@app.route("/auth/google/callback", methods=["POST"])
def callback():
    print("auth callback")

    # Get the ID token from the request body
    data = request.get_json()
    id_token_str = data.get("id_token")
    
    print("Got data", data)

    if not id_token_str:
        print("Missing ID token")
        return jsonify({"error": "Missing ID token"}), 400  # Handle missing ID token error

    try:
        # Verify the ID token
        id_info = id_token.verify_oauth2_token(id_token_str, requests.Request(), CLIENT_ID)

        # Extract user information
        user_id = id_info["sub"]
        email = id_info["email"]
        name = id_info["name"]
        picture = id_info["picture"]

        # Log in the user (this can involve storing user info in your database, session, etc.)
        # STOPPED HERE - we need to create a new session object, as the current one has the
        # wrong session identifier.
        sid = encrypt_session_identifier(user_id) # in url quoted version for the client
        session.sid = urllib.parse.unquote(sid)  # use unquoted version for storing
        session['user_id'] = user_id
        session['email'] = email
        session['name'] = name
        session['pic'] = picture

        print(f"Logging in {email}")
        auth_user(user_id, name, email, picture)

        # Return success message
        return jsonify({"message": "Login successful", 
                        "sid": sid,
                        "id_token": id_token_str,
                        "user": {"email": email, "name": name}}), 200

    except ValueError as e:
        # Invalid token
        print("Invalid id token",str(e))
        return jsonify({"error": "Invalid ID token"}), 400

@app.route('/authx/google', methods=['POST'])
def authx_google():
    print("***Auth/google")
    #print(request.headers)

    data = request.get_json()
    id_token_str = data.get('id_token')

    try:
        # Verify the token
        id_info = id_token.verify_oauth2_token(id_token_str, requests.Request(), CLIENT_ID)
        
        # Extract user information
        user_id = id_info['sub']
        email = id_info['email']
        name = id_info['name']
        pic = id_info['picture']
        print("Got google info")
        print(id_info)
        
        # You can add logic here to handle user info, e.g., saving to a database
        auth_user(user_id,name,email,pic)

        return jsonify({'message': 'User authenticated', 'user_id': user_id, 'email': email})

    except ValueError as e:
        # Invalid token
        return jsonify({'error': 'Invalid token', 'details': str(e)}), 400

@app.route("/")
def root():
    # For the sake of example, use static information to inflate the template.
    # This will be replaced with real information in later steps.
    print("***Root")
    #print(request.headers)
    sid = request.args.get('s')
    if sid is not None:
        return redirect(f"/home?s={sid}")
    else:
        return render_template("index.html")

@app.route("/home")
@login_required
def home():
    print("***Home")
    sid = request.args.get('s')
    print("Session ID", sid)
    if sid:
        user_id = decrypt_session_identifier(sid)
        print(f"found session id for {user_id}")
        if user_id is not None:
            session['user_id'] = user_id
            user = User.get(user_id)
            return render_template("home.html", email=user.email, name=user.name)
        else:
            print("Invalid session ID")
            return redirect(url_for('/'))
    print("redirecting from home to base - no session data")
    return redirect(url_for('/foo'))
    

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return jsonify({'message': 'User logged out', 'url': 'https://neurolens.scott.ai/'})

@login_manager.unauthorized_handler
def unauthorized():
    # do stuff
    print("Unauthorized... redirecting!")
    return redirect("/")

def user_code(user):
    return hashlib.md5(user.email.encode()).hexdigest()

# important!  set up cors access to the bucket
# 
# gsutil cors set config/cors_config.json gs://neuro-lens-bucket
#
def zip_user_photos(userid):
    bucket = storage_client.bucket(bucket_name)
    zpath = zip_path(userid)
    zuser = User.get(userid)
    
    # get a list of files in bucket
    pre = image_dir(userid)+"/"
    blobs = storage_client.list_blobs(bucket_name, prefix=pre, delimiter='/')
    
    # Zip the files
    udir = user_code(zuser)
    os.makedirs(ZIP_FOLDER+"/"+udir, exist_ok=True)
    local_zpath = os.path.join(ZIP_FOLDER, zpath)
    count = 0
    with zipfile.ZipFile(local_zpath, 'w') as zipf:
        for blob in blobs:
            count += 1
            file_path = os.path.join(UPLOAD_FOLDER, os.path.basename(blob.name))
            print("Downloading", blob.name, "to", file_path)
            blob.download_to_filename(file_path)
            zipf.write(file_path, os.path.basename(file_path))
            silent_remove(file_path)
    print(f"Created a zip with {count} files.")
    
    # # Upload the zip file to Google Cloud Storage
    print(f"Storing zip file {zpath}")
    blob = bucket.blob(zpath)
    blob.upload_from_filename(local_zpath)
    blob.make_public();
    # cleanup
    silent_remove(local_zpath)

    
@app.route("/photo_count")
@login_required
def photo_count():
    if current_user.is_authenticated:
        count = user_photo_count(current_user.id)
        return jsonify({'photo_count': count})
    else:
        return jsonify({'photo_count': 69})

    
def user_photo_count(userid):
    pre = image_dir(userid)+"/"
    print(f"prefix is {pre} of {bucket_name}")
    #bucket = storage_client.bucket(bucket_name)
    blobs = storage_client.list_blobs(bucket_name, prefix=pre, delimiter='/')
    #blobs = bucket.list_blobs(prefix=image_dir(userid), delimiter='/')
    #blobs = bucket.list_blobs()
    count = 0
    for blob in blobs:
        count += 1
    print(f"User has {count} photos")
    return count
    return sum(1 for _ in blobs)
    
@app.route("/my_data")
def my_data():
    user = User.get(current_user.id)
    return jsonify({'zip': user.photo_url, 'photo_count': user_photo_count(current_user.id)})

@app.route("/me")
@login_required
def me():
    # Get the user's profile information
    try:
        user_info = id_token.verify_oauth2_token(request.cookies.get('session'), requests.Request(), CLIENT_ID)
    except:
        user_info = None
    return jsonify({'user': user_info, 'code': user_code(current_user)})
    
def zip_path(userid):
    user = User.get(userid)
    udir = user_code(user)
    return os.path.join(udir, "uploaded_files.zip")

def image_dir(userid):
    user = User.get(userid)
    udir = user_code(user)
    return os.path.join(udir, "images")

def image_path(userid, filename):
    user = User.get(userid)
    udir = user_code(user)
    return os.path.join(udir, "images", filename)

def thumb_path(userid, filename):
    user = User.get(userid)
    udir = user_code(user)
    return os.path.join(udir, "thumbs", filename)

def thumb_dir(userid):
    user = User.get(userid)
    udir = user_code(user)
    return os.path.join(udir, "thumbs")

@app.route('/zip/<userzip>')
def zip_user(userzip):
    userid = userzip.split(".")[0]
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(zip_path(userid))
    
    # see if blob exists, if it doesn't zip up photos
    if not blob.exists():
        zip_user_photos(userid)

    # return zip file contents
    return redirect(blob.public_url)

def silent_remove(filename):
    try:
        os.remove(filename)
    except OSError:
        pass
 
def AddPromptToImage(prompt, file_path):
    if prompt is None:
        print(f"No prompt to save for {file_path}")
        return
    # store prompt string in the extensions of PNG file file_path
    image = Image.open(file_path)

    # Convert the dictionary to a JSON string for storage
    metadata_json = json.dumps({'prompt': prompt})

    # Add the metadata using the PngInfo class
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("custom_metadata", metadata_json)

    # Save the image with the metadata
    image.save(file_path, pnginfo=png_info)
    print(f"Prompt saved to {file_path}: {prompt}")
    
def GetPromptFromImage(file_path):
    # Open the image file
    image = Image.open(file_path)

    try:
        # Extract the metadata from the PNG image
        metadata_info = image.info.get("custom_metadata")
        if metadata_info is not None:
            # Load the JSON metadata into a dictionary
            metadata = json.loads(metadata_info)
            print(f"Found custom metadata in {file_path}: {metadata}")
            # Return the prompt from the metadata
            return metadata.get("prompt","")
        else:
            print(f"No custom metadata found in {file_path}")
            return ""
    except:
        return ""
    
# Ensure the /tmp/uploads directory exists
UPLOAD_FOLDER = '/tmp/uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

@app.route('/read_prompt', methods=['POST'])
def handle_file_upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400

    file = request.files['file']

    # Ensure a filename is provided
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400

    # Save the file to /tmp/uploads
    filepath = os.path.join(UPLOAD_FOLDER, file.filename)
    try:
        file.save(filepath)
    except Exception as e:
        return jsonify({'error': 'Could not save file', 'details': str(e)}), 500

    # Call the read_prompt function to extract the prompt from the file
    prompt_string = GetPromptFromImage(filepath)

    # Return the prompt string in JSON format
    return jsonify({'prompt': prompt_string})
    
def process_image_file(uid, bucket, file_path, prompt=None, job=None):
    # clean up an image stored locally at file_path and then
    # upload the 1024x1024 and thumb to cloud storage
    image_id = str(uuid.uuid4()) if job is None else job
    processed_file = change_extension_to_png(os.path.basename(file_path))
    processed_thumb = "thumb_"+processed_file
    processed_path = os.path.join(PROCESSED_FOLDER, processed_file)
    local_thumb_path = os.path.join(PROCESSED_FOLDER, processed_thumb)
    print(f"Processing {file_path} to {processed_path}")
    resize_image_to_square(file_path, processed_path)
    create_square_thumbnail(file_path, local_thumb_path)
    print(f"Removing {file_path}")
    silent_remove(file_path)
    
    # Save prompts
    AddPromptToImage(prompt, processed_path)
    AddPromptToImage(prompt, local_thumb_path)
        
    print("Saving image to cloud")
    # Upload the processed file to Google Cloud Storage
    blob = bucket.blob(image_path(uid, processed_file))
    blob.upload_from_filename(processed_path)
    blob.make_public()
    image_url = blob.public_url
            
    print("Saving thumbnail to cloud")
    # Upload the thumbnail to Google Cloud Storage
    blob = bucket.blob(thumb_path(uid, processed_thumb))
    blob.upload_from_filename(local_thumb_path)
    blob.make_public()
    thumb_url = blob.public_url
    
    # store metadata in /images of firestore
    update_image(uid, image_id, {'image_url': image_url, 'thumb_url': thumb_url})

    #cleanup
    print("Cleaning up")
    silent_remove(processed_path)
    silent_remove(local_thumb_path)
    return {'image_url': image_url, 'thumb_url': thumb_url}

@app.route('/upload', methods=['POST'])
def upload_file():
    print("***Upload")
    #print(request.headers)
    if session.get('user_id') is not None:
        uid = session['user_id']
        print("User ID: "+uid)
    else:
        print("No user id")
    print("current user:")
    print(current_user)
    if 'filepond' in request.files:
        files = request.files.getlist('filepond')
        file_paths = []
        bucket = storage_client.bucket(bucket_name)
        zip_dir = user_code(current_user)

        # Save the uploaded images
        for file in files:
            file_path = os.path.join(UPLOAD_FOLDER, file.filename)
            file.save(file_path)
            process_image_file(current_user.id, bucket, file_path)

        # Return the public URL of the uploaded file
        uid = current_user.id
        public_url = f"https://neurolens.scott.ai/zip/{uid}"
        User.update_photo_url(uid, public_url)
        return jsonify({'message': 'Files uploaded and zipped successfully', 'url': public_url}), 200
    else:
        return jsonify({'message': 'No files uploaded'}), 400
    
def change_extension_to_png(file_path):
    # Split the file path into root and extension
    root, _ = os.path.splitext(file_path)
    
    # Create a new file path with the .png extension
    new_file_path = root + '.png'
    
    return new_file_path

def resize_image_to_square(input_image_path, output_image_path, size=1024):
    # Open the image file
    with Image.open(input_image_path) as img:
        # Ensure the image has an alpha channel for transparency
        img = img.convert("RGBA")
        
        # Resize the image while maintaining aspect ratio
        img.thumbnail((size, size), Image.LANCZOS)

        # Create a new square image with a transparent background
        new_img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

        # Calculate the position to paste the resized image on the square canvas
        paste_position = ((size - img.width) // 2, (size - img.height) // 2)
        
        # Paste the resized image onto the center of the square canvas
        new_img.paste(img, paste_position, img)

        # Save the final image as PNG with transparency
        new_img.save(output_image_path, format='PNG')

def create_thumbnail(input_image_path, output_image_path, size=(228, 228)):
    # Open the image file
    with Image.open(input_image_path) as img:
        # Convert the image to RGBA mode to ensure it supports transparency
        img = img.convert("RGBA")

        # Create a thumbnail while maintaining the aspect ratio
        img.thumbnail(size, Image.LANCZOS)

        # Create a new blank image with a transparent background
        thumb = Image.new('RGBA', size, (0, 0, 0, 0))

        # Calculate the position to paste the thumbnail
        paste_position = ((size[0] - img.width) // 2, (size[1] - img.height) // 2)
        
        # Paste the thumbnail onto the center of the new transparent image
        thumb.paste(img, paste_position, img)

        # Save the final thumbnail as a PNG to retain transparency
        thumb.save(output_image_path, format='PNG')
        
def create_square_thumbnail(input_image_path, output_image_path, size=228):
    """
    Create a square thumbnail of the specified size from the input image and save it to the output path.

    Args:
        input_image_path (str): Path to the input image file.
        output_image_path (str): Path where the thumbnail will be saved.
        size (int): The size (width and height) of the square thumbnail. Default is 228.
    """
    try:
        # Open the image
        with Image.open(input_image_path) as image:
            image = image.convert("RGBA")
            width, height = image.size
            
            # Determine the crop box for the largest square
            if width > height:  # Landscape
                left = (width - height) // 2
                crop_box = (left, 0, left + height, height)
            elif height > width:  # Portrait
                top = 0
                crop_box = (0, top, width, top + width)
            else:  # Square
                crop_box = (0, 0, width, height)

            # Crop the image to the largest square
            image = image.crop(crop_box)

            # Resize the cropped image to the desired size
            image = image.resize((size, size), Image.LANCZOS)

            # Save the thumbnail to the output path
            image.save(output_image_path, format='PNG')

    except Exception as e:
        print(f"An error occurred: {e}")

def make_model(name):
    try:
         model = replicate.models.create(
                owner=REPLICATE_USER,
                name=name,
                visibility="public",
                hardware="gpu-a40-large"
         )
    except Exception as e:
        print(e)
    return f"{REPLICATE_USER}/{name}"

# TODO only allow training if there are images, and a training isn't already in progress

def valid_model(user):
    m = user.model
    if m is not None and m.get('status', None) in ['success', 'succeeded']:
        return True
    return False

@app.route('/state')
@login_required
def state():
    updated = User.get(current_user.id)  # refresh
    if user_photo_count(updated.id) < 20:  
        ans = 'upload'
    elif valid_model(updated):
        ans = 'ready'
    elif updated.training_data is None:
        ans = 'submit'
    else:
        ans = 'training'
    #ans = 'ready' # testing TODO
    return nocache_json({'state': ans})

def get_train_info(training):
    base = training.urls
    base['id'] = training.id
    base['destination'] = training.destination
    base['created-at'] = training.created_at
    return base

@app.route('/train')
@login_required
def train():
    code = user_code(current_user)
    model = make_model(f"flux-dev-lora-{code}")
    webhook = f"https://neurolens.scott.ai/train_update/{current_user.id}"
    input_images = f"https://neurolens.scott.ai/zip/{current_user.id}.zip"
    print("webhook", webhook)
    print("input_images", input_images)
    print("model", model)
    #return jsonify({'message': 'Training started', 'model': model})
    training = replicate.trainings.create(
    # You need to create a model on Replicate that will be the destination for the trained version.                        
        destination=model,
        version="ostris/flux-dev-lora-trainer:7f53f82066bcdfb1c549245a624019c26ca6e3c8034235cd4826425b61e77bec",
        webhook=webhook,
        webhook_event_filter=["start","output","logs","completed"],
        input={
            "steps": 1000,
            "lora_rank": 16,
            "optimizer": "adamw8bit",
            "batch_size": 1,
            "resolution": "512,768,1024",
            "autocaption": True,
            "input_images": input_images,
            "trigger_word": "me:-)",
            "learning_rate": 0.0004
        },
    )
    User.update_training(current_user.id,get_train_info(training))
    return jsonify({'message': 'Training started', 'id': training.id})

@app.route('/train_update/<userid>', methods=['POST'])
def train_complete(userid):
    j = request.get_json()
    feedback_id = j.get('id', None)
    print(f"Training update for job {feedback_id}!")
    print(j)
    u = User.get(userid)
    try:
        training_id = u.training_data.get('id', None)
        status = j['status']
        if training_id != feedback_id:
            print("Older training, ignoring.")
            return jsonify({'message': 'Training not active'})
        elif status in ['succeeded','success']:
            User.update_model(u.id, j)
            return jsonify({'message': 'Training complete'})
        else:
            print(f"training update {status}")
    except Exception as e:
        print(f"Error checking training, ignoring feedback.\n{e}")
        return jsonify({'message': 'Training not found'})

@app.route('/clear')
@login_required
def clear():
    try:
        delete_blob(bucket_name, zip_path(current_user.id))
    except:
        pass
    return jsonify({'message': 'zip deleted'})

@app.route('/reset')
@login_required
def reset():
    # erase zip
    clear()
    
    # TODO delete dict elements in firestore
    
    # Delete all images and thumbnails
    blobs = storage_client.list_blobs(bucket_name, prefix=image_dir(current_user.id)+"/", delimiter='/')
    for blob in blobs:
        delete_metadata(blob.name)
        blob.delete()
    blobs = storage_client.list_blobs(bucket_name, prefix=thumb_dir(current_user.id)+"/", delimiter='/')
    for blob in blobs:
        delete_metadata(blob.name)
        blob.delete()
    current_user.reset()
    return jsonify({'message': 'All images and thumbnails deleted'})

def delete_blob(bucket_name, blob_name):
    """Deletes a blob from the bucket."""
    # bucket_name = "your-bucket-name"
    # blob_name = "your-object-name"

    storage_client = storage.Client()

    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    generation_match_precondition = None

    # Optional: set a generation-match precondition to avoid potential race conditions
    # and data corruptions. The request to delete is aborted if the object's
    # generation number does not match your precondition.
    blob.reload()  # Fetch blob metadata to use in generation_match_precondition.
    generation_match_precondition = blob.generation

    blob.delete(if_generation_match=generation_match_precondition)
    delete_metadata(blob_name)

    print(f"Blob {blob_name} deleted.")
    
@app.route('/kill/<path:path>')
def kill(path):
    # first, delete the thumbnail this represents thumb_photo.png
    try:
        delete_blob(bucket_name, path)
    except:
        print("Thumbnail not found")
    
    # TODO delete the image metadata from firestore
    # now delete the source image photo.png
    file = os.path.basename(path)
    parts = file.split("_")
    source = '_'.join(parts[1:])
    try:
        delete_blob(bucket_name, image_path(current_user.id, source))
        return jsonify({'status': 'success', 'message': 'Image deleted'})
    except Exception as oops:
        print("Source image not found")
        return jsonify({'status': 'error', 'message': f"Error deleting image: {oops}"})
    
#
## Photo Grid
#

def generate_image_stream(blob):
    """Generator that streams the image in chunks."""
    chunk_size = 1024 * 1024  # 1 MB per chunk
    with blob.open("rb") as image_file:
        while True:
            chunk = image_file.read(chunk_size)
            if not chunk:
                break
            yield chunk
            
def generate_zip_stream(blob):
    return generate_image_stream(blob)

def kill_photo(index,img,kill,view):
    base = """
    <div id="gallery_image_{}" class="relative flex justify-center items-center">
    <img class="gallery-image max-w-full rounded-lg" src="{}" data-large="{}" alt="">

    <!-- Larger touch area for the "X" button (44x44 pixels) -->
    <div class="absolute top-0 right-0 w-11 h-11 flex items-center justify-center">
        <div class="kill text-xl font-bold cursor-pointer" data-image-id="gallery_image_{}" data-url="{}">&times;</div>
    </div>
    </div>
    """
    return base.format(index,img,view,index,kill)

@app.route("/photo/<path:path>")
def photo(path):
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(path)
    if blob.exists():
        content_type, _ = mimetypes.guess_type(blob.name)
        # Create a streaming response with the image data
        response = Response(generate_image_stream(blob), content_type=content_type)
        
        # Set cache headers for browser caching (e.g., cache for 1 week)
        response.headers['Cache-Control'] = 'public, max-age=604800'  # 1 week in seconds
        response.headers['Content-Type'] = content_type
        
        return response, 200
    else:
        return "No such photo", 404
    
def photo_from_thumb(path):
    p1 = path.replace("thumbs","images")
    return p1.replace("thumb_","")

# TODO we need a clean image abstraction  / class
def image_urls(userid, thumb_blob_name):
    filename = os.path.basename(thumb_blob_name)
    photo = photo_from_thumb(filename)
    tpath = thumb_path(userid,filename)
    base = "https://storage.googleapis.com/neuro-lens-bucket"
    # we return the thumb image, the kill switch, the full view
    return [f"{base}/{tpath}",
            f"/kill/{tpath}", 
            f"{base}/{image_path(userid,photo)}"]

# TODO have /grid pull from metadata, vs directory, then provide the image_id so that
# we can delete the image appropriately

@app.route("/grid")
@login_required
def photo_grid():
    print("***Grid")
    #print(request.headers)
    user_id = current_user.id
    bucket = storage_client.bucket(bucket_name)
    blobgen = storage_client.list_blobs(bucket_name, prefix=thumb_dir(user_id)+"/", delimiter='/')
    blobs = [_ for _ in blobgen]
    
    # Sort blobs by their updated time, in descending order (most recent first)
    blobs_sorted = sorted(blobs, key=lambda blob: blob.updated, reverse=True)

    names = [blob.name for blob in blobs_sorted]
    images = [image_urls(user_id,name) for name in names]
    out= "<div id='gallery-images' class='grid grid-cols-2 md:grid-cols-3 gap-4'>"
    index = 0
    for imgkill in images:
        img = imgkill[0]
        index += 1
        # we get thumb image, kill link, full view
        out += kill_photo(index,imgkill[0],imgkill[1],imgkill[2])
    out += "</div>"
    return out

#
## Image generation
#

@app.route('/genImage', methods=['POST'])
@login_required
def gen_image_post():
    print("***Gen Image")
    #print(request.headers)

    data = request.get_json()
    print("Got json", data)
    job = genImage(current_user, data.get('prompt'))
    return jsonify({'message': 'Image generation started', 'job': job})

def latest_replicate_model_version(user):
    base_model = f"scottspace/flux-dev-lora-{user_code(user)}"
    model = replicate.models.get(base_model)
    versions = model.versions.list()
    # versions is a page object, which we sort in descending order by creation date 
    sorted(versions,key=lambda x: x.dict()['created_at']).reverse()
    return versions[0]

# TODO images need to be a class

def create_image(user, prompt):
    ## create a structure in firestore to store image data
    user_id = user.id
    image_id = user.image_job
    doc_ref = db.collection('images').document(image_id)
    doc = doc_ref.get()
    if doc.exists:
       info = doc.to_dict()
    else:
       info = {'user_id': user_id, 
               'image_id': image_id}
    info['created'] = time.time()
    info['prompt'] = prompt
    info['thumb_url'] = None
    info['image_url'] = None
    info['filename'] = None
    doc_ref.set(info)  
    
def delete_metadata(path):
    # docs can't have / in their name, so we replace them with _
    path = path.replace("/", "_")
    doc_ref = db.collection('image_map').document(path)
    doc = doc_ref.get()
    if doc.exists:
       info = doc.to_dict()
       img_doc_ref = db.collection('images').document(info['image_id'])
       img_doc = img_doc_ref.get()
       if img_doc.exists:
           img_doc_ref.delete()
       doc_ref.delete()
    
def add_to_image_map(url, image_id):
    # docs can't have / in their name, so we replace them with _
    path = urlparse(url).path.replace("/", "_")
    doc_ref = db.collection('image_map').document(path)
    doc = doc_ref.get()
    if doc.exists:
       info = doc.to_dict()
       info['image_id'] = image_id
    else:
       info = {'image_id': image_id,
               'id': path}
    doc_ref.set(info)
    
def update_image(user_id, image_id, url_dict): 
    doc_ref = db.collection('images').document(image_id)
    doc = doc_ref.get()
    if doc.exists:
       info = doc.to_dict()
    else:
       info = {'user_id': user_id, 
               'image_id': image_id}
    info['thumb_url'] = url_dict['thumb_url']
    info['image_url'] = url_dict['image_url']
    info['created'] = time.time()
    info['filename'] = os.path.basename(url_dict['image_url'])
    doc_ref.set(info)  
    # URLs are of the form
    # https://storage.googleapis.com/neuro-lens-bucket/3507430c674561ac8f35c84815a8d8fd/images/IMG_8053.png
    add_to_image_map(info['thumb_url'], image_id)
    add_to_image_map(info['image_url'], image_id)
    
def genImage(user, prompt):
    print("Resettting status")
    user.image_job = str(uuid.uuid4())
    user.image_job_log = None
    user.image_job_status = "requested"
    user.image_job_output = None
    user.save()
    create_image(user, prompt)
    v = latest_replicate_model_version(user)
    print("Trying model version", v.id)
    webhook = f"https://neurolens.scott.ai/image_update/{user.id}/{user.image_job}"
    prediction = replicate.predictions.create(
        version=latest_replicate_model_version(user),
        input={"prompt":str(prompt),
               "output_format": "png",
               "width": 1024,
               "height": 1024,
               "num_outputs": 1,
               "image_job": user.image_job},
        webhook=webhook,
        webhook_event_filter=["start","output","logs","completed"])
    user.image_job_status = prediction.status
    user.save()
    print("Started prediction", prediction)
    return user.image_job 

#
## Webhook for images
#

def nocache_json(j):
    response = make_response(jsonify(j))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

# this is pinged frequently by the web ui
@app.route('/image_status')
@login_required
def image_status():
    user = User.get(current_user.id) #important to refresh!
    print("status for {}".format(user.email))
    return nocache_json({
        'status': user.image_job_status, 
        'job': user.image_job,
        'output': user.image_job_output,
        'log': user.image_job_log})

# replicate posts back here with updates on the image
@app.route("/image_update/<userid>/<job>", methods=['POST'])
def image_update(userid, job):
    try:
        info = request.get_json(silent=True)
        user = User.get(str(userid))
        print("webhook ",info)
        if info is None:
            print("No json")
            return jsonify({'error': 'no json'})
        # TODO deal with multiple image jobs, multiple images
        user.image_job_status = info.get('status',None)
        # check for erorr
        err = info.get('error',None)
        if err:
            user.image_job_log = err
            user.image_job_status = 'error'
            user.save()
            return jsonify({'error': err})
        imgs = get_images(info)
        if imgs:
            user.image_job_output = {'images': info.get('urls',None)}
            print("Images are ready", imgs)
            copy_images_locally(user, job, imgs)
        user.save()
        return jsonify({'success': 0})
    except Exception as e:
        user.image_job_status = 'error'
        user.save()
        print("Image Hook Exception", e)
        return jsonify({'error': str(e)}) #TODO make this string json clean 
    
def lookup_image_job_prompt(image_job):
    doc_ref = db.collection('images').document(image_job)
    doc = doc_ref.get()
    if doc.exists:
       info = doc.to_dict()
       return info.get('prompt',None)
    return None 

def copy_images_locally(user, image_job, urls):
    # handle errors TODO
    userid = user.id
    bucket = storage_client.bucket(bucket_name)
    for url in urls:
        filename = unique_filename(url)
        local_path = os.path.join(UPLOAD_FOLDER, filename)
        print("Downloading", url, "to", local_path)
        r = webrequests.get(url, stream=True)
        with open(local_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        # TODO store prompt in the png image :-)
        prompt = lookup_image_job_prompt(image_job)
        process_image_file(userid, bucket, local_path, prompt, image_job)
        
def tz_now():
   n = datetime.now(timezone.utc)
   n = n.astimezone(get_localzone())
   return n

def get_extension(url):
    parsed_url = urlparse(url)
    path = parsed_url.path
    filename = os.path.basename(path)
    _, file_extension = os.path.splitext(filename)
    return file_extension

def unique_filename(url):
    md5 = hashlib.md5(url.encode()).hexdigest() 
    ext = get_extension(url)
    return "{}{}".format(md5,ext)

def sd_success(info):
    return info.get('status',None) in ['success','succeeded']

def sd_processing(info):
    return info.get('status',None) == 'processing'

def get_images(info):
    if sd_success(info):
        print("SD success: {}".format(info))
        all = info.get('output', None)
        if all and all is not None and len(all) > 0:
            if isinstance(all,str):
                # replicate may do this
                return [all]
            return all 
    return None

# Generate a key (you should generate and store this securely for your app)

# Load the encryption key from an environment variable
def get_encryption_key():
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise ValueError("No encryption key found in environment variable 'ENCRYPTION_KEY'")
    return key.encode()  # Convert the key from a string to bytes

# Encrypt the session identifier
def encrypt_session_identifier(user_id):
    encryption_key = get_encryption_key()  # TODO use one per session soon
    random_number = str(os.urandom(4).hex())  # 4 bytes = 8 hex digits (random number)
    identifier = f"{random_number}/{user_id}"
    cipher = Fernet(encryption_key)
    encrypted_identifier = cipher.encrypt(identifier.encode())
    url_safe_encrypted_identifier = urllib.parse.quote(encrypted_identifier)
    return url_safe_encrypted_identifier

# Decrypt the session identifier
def decrypt_session_identifier(encrypted_identifier):
    # URL decode the identifier
    encryption_key = get_encryption_key() # TODO use one per session soon
    try:
        url_decoded_encrypted_identifier = urllib.parse.unquote(encrypted_identifier)
        cipher = Fernet(encryption_key)
        decrypted_identifier = cipher.decrypt(url_decoded_encrypted_identifier.encode()).decode()
        _, user_id = decrypted_identifier.split("/")
    except:
        print("*Bad session string*")   
        user_id = None
    return user_id

if __name__ == "__main__":
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    # Flask's development server will automatically serve static files in
    # the "static" directory. See:
    # http://flask.pocoo.org/docs/1.0/quickstart/#static-files. Once deployed,
    # App Engine itself will serve those files as configured in app.yaml.
    app.run(host="127.0.0.1", port=8080, debug=True)
