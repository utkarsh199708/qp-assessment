package com.grocery.booking.grocery.Controller;

import com.grocery.booking.grocery.DTO.InventoryManagementRequest;
import com.grocery.booking.grocery.Entity.GroceryItem;
import com.grocery.booking.grocery.Service.GroceryItemService;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.List;

@RestController
@RequestMapping("/api/admin/grocery")
public class GroceryItemController {
    @Autowired
    private GroceryItemService groceryItemService;

    @GetMapping("/get")
    public List<GroceryItem> getAllGroceryItems() {
        return groceryItemService.getAllGroceryItems();
    }

    @PostMapping
    public ResponseEntity<GroceryItem> addGroceryItem( @RequestBody GroceryItem groceryItem) {
        GroceryItem savedItem = groceryItemService.addGroceryItem(groceryItem);
        return new ResponseEntity<>(savedItem, HttpStatus.CREATED);
    }

    @PutMapping("/{id}")
    public ResponseEntity<GroceryItem> updateGroceryItem(
            @PathVariable Long id,
             @RequestBody GroceryItem updatedItem) {
        GroceryItem updated = groceryItemService.updateGroceryItem(id, updatedItem);
        return new ResponseEntity<>(updated, HttpStatus.OK);
    }

    @DeleteMapping("/{id}")
    public ResponseEntity<Void> deleteGroceryItem(@PathVariable Long id) {
        groceryItemService.deleteGroceryItem(id);
        return new ResponseEntity<>(HttpStatus.NO_CONTENT);
    }

    @PatchMapping("/{id}/manage-inventory")
    public ResponseEntity<GroceryItem> manageInventory(
            @PathVariable Long id,
            @RequestBody InventoryManagementRequest request) {
        GroceryItem updated = groceryItemService.manageInventory(id, request);
        return new ResponseEntity<>(updated, HttpStatus.OK);
    }

    // Other endpoints for updating, deleting, managing inventory, etc.
}
